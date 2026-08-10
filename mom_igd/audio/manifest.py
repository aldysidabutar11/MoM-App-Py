"""Append-only recording manifest, and its verification.

The manifest is the authoritative on-disk record of what was captured. The
database mirrors it for querying, but if the two ever disagree the manifest wins:
it is written next to the audio, in the same directory, by the same thread that
wrote the audio, and appended with an ``fsync`` before the corresponding database
transaction commits.

Format: **JSON Lines**. One self-contained JSON object per line, appended and
flushed. That choice is deliberate -- a single JSON document would have to be
rewritten on every chunk, and a crash during that rewrite would destroy the
record of every chunk before it. With JSON Lines a crash can only ever damage the
final line, which is detectable and discardable without losing anything earlier.

Every chunk record carries enough information to verify the audio independently
of the database: sequence, filename, frame range, both clocks, format, byte count
and SHA-256. :func:`verify_manifest` recomputes the hashes from disk, so a
modified chunk or a tampered manifest is detected rather than trusted.
"""

from __future__ import annotations

import hashlib
import json
import os
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Iterable, Iterator, Sequence

from mom_igd.audio.backend import CaptureProfile, SampleFormat

__all__ = [
    "CHUNK_FILENAME_TEMPLATE",
    "ChunkRecord",
    "ChunkStatus",
    "ManifestError",
    "ManifestWriter",
    "MANIFEST_FILENAME",
    "MANIFEST_SUMMARY_FILENAME",
    "RecoveryStatus",
    "VerificationReport",
    "chunk_filename",
    "compute_chain_hash",
    "read_manifest",
    "sha256_file",
    "verify_manifest",
    "write_manifest_summary",
]

MANIFEST_FILENAME: Final[str] = "manifest.jsonl"
MANIFEST_SUMMARY_FILENAME: Final[str] = "manifest.json"
CHUNK_FILENAME_TEMPLATE: Final[str] = "chunk_{seq:06d}.wav"
PARTIAL_SUFFIX: Final[str] = ".pcm.part"
CHUNK_META_SUFFIX: Final[str] = ".meta.json"
QUARANTINE_DIRNAME: Final[str] = "quarantine"

_MANIFEST_VERSION: Final[int] = 1
_HASH_BLOCK: Final[int] = 1024 * 1024


class ManifestError(RuntimeError):
    """Raised when the manifest is missing, malformed or inconsistent."""


class ChunkStatus(StrEnum):
    """Lifecycle of one chunk file. Mirrors ``recording_chunks.status``."""

    WRITING = "WRITING"
    WRITTEN = "WRITTEN"
    TRUNCATED = "TRUNCATED"
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"
    QUARANTINED = "QUARANTINED"


class RecoveryStatus(StrEnum):
    """How this chunk came to exist."""

    NONE = "NONE"
    """Written normally during capture."""

    RECOVERED = "RECOVERED"
    """Rebuilt from a partial file after an interrupted recording."""

    RECOVERY_FAILED = "RECOVERY_FAILED"
    """A partial file existed but could not be turned into valid audio."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def chunk_filename(seq: int) -> str:
    """Bare, zero-padded filename for a chunk. Contains no personal data.

    Names are derived from a sequence number only. Neither the meeting title nor
    any participant name ever appears in a path: file names leak to backups,
    file pickers and error messages.
    """
    if seq < 0:
        raise ManifestError(f"Chunk sequence must be >= 0, got {seq}.")
    return CHUNK_FILENAME_TEMPLATE.format(seq=seq)


def sha256_file(path: Path, *, block_size: int = _HASH_BLOCK) -> str:
    """Stream a file through SHA-256 without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """One finalised chunk, as recorded in the manifest."""

    seq: int
    filename: str
    start_frame: int
    end_frame: int
    frame_count: int
    duration_ms: float
    utc_start: str
    utc_end: str
    monotonic_start_ns: int
    monotonic_end_ns: int
    sample_rate: int
    channels: int
    sample_format: str
    byte_count: int
    sha256: str
    xrun_callbacks: int = 0
    dropped_frames: int = 0
    status: str = ChunkStatus.WRITTEN.value
    recovery_status: str = RecoveryStatus.NONE.value
    finalized: bool = True
    peak_dbfs: float | None = None
    rms_dbfs: float | None = None
    clipped_samples: int = 0
    notes: str | None = None

    @property
    def monotonic_duration_ms(self) -> float:
        return (self.monotonic_end_ns - self.monotonic_start_ns) / 1_000_000.0

    @property
    def is_usable_audio(self) -> bool:
        return self.status in {ChunkStatus.WRITTEN.value, ChunkStatus.TRUNCATED.value}

    def chain_line(self) -> str:
        """The canonical string this chunk contributes to the chain hash."""
        return f"{self.seq}:{self.frame_count}:{self.byte_count}:{self.sha256}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["type"] = "chunk"
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChunkRecord:
        data = {k: v for k, v in payload.items() if k != "type"}
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 - dataclass API
        unknown = set(data) - known
        for key in unknown:
            data.pop(key)
        try:
            return cls(**data)
        except TypeError as exc:
            raise ManifestError(f"Malformed chunk record: {exc}") from exc


def compute_chain_hash(records: Sequence[ChunkRecord]) -> str:
    """Hash the ordered chunk list.

    Changing any chunk's hash, frame count, byte count or position changes this
    value, so the summary detects a manifest that has been edited to match a
    tampered chunk file.
    """
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item.seq):
        digest.update(record.chain_line().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


class ManifestWriter:
    """Appends records to ``manifest.jsonl``, durably.

    Called only from the single writer thread. Each append is flushed and
    ``fsync``ed before the caller commits the matching database transaction, so a
    crash can leave the manifest ahead of the database but never behind it. An
    extra manifest line whose chunk the database does not know about is
    recoverable; a database row pointing at a chunk with no manifest record is
    not.
    """

    def __init__(self, directory: Path) -> None:
        self._path = directory / MANIFEST_FILENAME
        self._directory = directory

    @property
    def path(self) -> Path:
        return self._path

    def append(self, payload: dict[str, Any]) -> None:
        """Append one record and force it to disk."""
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append_chunk(self, record: ChunkRecord) -> None:
        self.append(record.to_dict())

    def append_event(self, event: str, **fields: Any) -> None:
        """Record something that is not a chunk: a gap, a pause, a recovery step."""
        payload: dict[str, Any] = {"type": event, "utc": utc_now_iso()}
        payload.update(fields)
        self.append(payload)


def write_manifest_summary(
    directory: Path,
    *,
    recording_uuid: str,
    meeting_uuid: str,
    profile: CaptureProfile,
    records: Sequence[ChunkRecord],
    device: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    counters: dict[str, Any] | None = None,
    gaps: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Write ``manifest.json``, the verifiable summary of a finished recording."""
    usable = [r for r in records if r.is_usable_audio]
    summary: dict[str, Any] = {
        "manifest_version": _MANIFEST_VERSION,
        "recording_uuid": recording_uuid,
        "meeting_uuid": meeting_uuid,
        "generated_utc": utc_now_iso(),
        "profile": profile.describe(),
        "chunk_count": len(records),
        "usable_chunk_count": len(usable),
        "total_frames": sum(r.frame_count for r in usable),
        "total_bytes": sum(r.byte_count for r in usable),
        "duration_seconds": round(
            sum(r.frame_count for r in usable) / profile.sample_rate, 3
        ),
        "chain_sha256": compute_chain_hash(records),
        "chunks": [r.to_dict() for r in sorted(records, key=lambda item: item.seq)],
        "gaps": list(gaps),
        "device": device or {},
        "quality": quality or {},
        "counters": counters or {},
    }
    target = directory / MANIFEST_SUMMARY_FILENAME
    temporary = target.with_suffix(".json.tmp")
    payload = json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=False)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return summary


# ---------------------------------------------------------------------------
# Reading and verification
# ---------------------------------------------------------------------------


def read_manifest(directory: Path) -> tuple[list[ChunkRecord], list[dict[str, Any]], int]:
    """Read ``manifest.jsonl``.

    Returns:
        ``(chunks, events, torn_lines)``. A trailing line that is not valid JSON
        is counted rather than raised: it is the expected signature of a crash
        mid-append, and everything before it is still trustworthy.
    """
    path = directory / MANIFEST_FILENAME
    if not path.is_file():
        return [], [], 0

    chunks: list[ChunkRecord] = []
    events: list[dict[str, Any]] = []
    torn = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                torn += 1
                continue
            if not isinstance(payload, dict):
                torn += 1
                continue
            if payload.get("type") == "chunk":
                chunks.append(ChunkRecord.from_dict(payload))
            else:
                events.append(payload)
    return chunks, events, torn


@dataclass(slots=True)
class VerificationReport:
    """Result of verifying a recording directory against its manifest."""

    directory: str
    chunk_count: int = 0
    verified_chunks: int = 0
    total_frames: int = 0
    total_bytes: int = 0
    duration_seconds: float = 0.0
    torn_manifest_lines: int = 0
    chain_sha256: str = ""
    summary_chain_sha256: str | None = None
    problems: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    checksum_mismatches: list[str] = field(default_factory=list)
    header_mismatches: list[str] = field(default_factory=list)
    unexpected_files: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "ok": self.ok,
            "chunk_count": self.chunk_count,
            "verified_chunks": self.verified_chunks,
            "total_frames": self.total_frames,
            "total_bytes": self.total_bytes,
            "duration_seconds": round(self.duration_seconds, 3),
            "torn_manifest_lines": self.torn_manifest_lines,
            "chain_sha256": self.chain_sha256,
            "summary_chain_sha256": self.summary_chain_sha256,
            "problems": list(self.problems),
            "missing_files": list(self.missing_files),
            "checksum_mismatches": list(self.checksum_mismatches),
            "header_mismatches": list(self.header_mismatches),
            "unexpected_files": list(self.unexpected_files),
            "gaps": list(self.gaps),
        }


def verify_manifest(directory: Path, *, check_headers: bool = True) -> VerificationReport:
    """Verify every chunk in a recording directory against the manifest.

    Recomputes each chunk's SHA-256 from disk, checks the WAV header agrees with
    the recorded format and frame count, and recomputes the chain hash. Detects a
    modified chunk, a modified manifest, a missing chunk and a sequence gap.
    """
    report = VerificationReport(directory=directory.name)
    if not directory.is_dir():
        report.problems.append(f"recording directory does not exist: {directory.name}")
        return report

    records, events, torn = read_manifest(directory)
    report.torn_manifest_lines = torn
    report.chunk_count = len(records)
    if torn:
        report.problems.append(
            f"{torn} manifest line(s) are unreadable (expected after a crash "
            "mid-append; the records before them remain valid)"
        )
    if not records:
        report.problems.append("manifest contains no chunk records")
        return report

    report.gaps = [e for e in events if e.get("type") == "gap"]
    report.chain_sha256 = compute_chain_hash(records)

    summary_path = directory / MANIFEST_SUMMARY_FILENAME
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            report.summary_chain_sha256 = str(summary.get("chain_sha256", "")) or None
        except (json.JSONDecodeError, OSError) as exc:
            report.problems.append(f"{MANIFEST_SUMMARY_FILENAME} is unreadable: {exc}")
        if (
            report.summary_chain_sha256
            and report.summary_chain_sha256 != report.chain_sha256
        ):
            report.problems.append(
                "chain hash mismatch: manifest.json records "
                f"{report.summary_chain_sha256[:12]}... but the chunk records hash to "
                f"{report.chain_sha256[:12]}... (the manifest or a chunk record was "
                "altered after the recording was finalised)"
            )

    sequences = sorted(r.seq for r in records)
    duplicates = {s for s in sequences if sequences.count(s) > 1}
    if duplicates:
        report.problems.append(f"duplicate chunk sequence(s): {sorted(duplicates)}")
    expected = list(range(sequences[0], sequences[0] + len(set(sequences))))
    if sorted(set(sequences)) != expected:
        report.problems.append(
            f"chunk sequence is not contiguous: {sorted(set(sequences))}"
        )

    known_files = {MANIFEST_FILENAME, MANIFEST_SUMMARY_FILENAME}
    for record in sorted(records, key=lambda item: item.seq):
        known_files.add(record.filename)
        path = directory / record.filename
        if not path.is_file():
            if record.status != ChunkStatus.MISSING.value:
                report.missing_files.append(record.filename)
                report.problems.append(
                    f"chunk {record.seq} file is missing: {record.filename}"
                )
            continue
        actual_size = path.stat().st_size
        if actual_size != record.byte_count:
            report.problems.append(
                f"chunk {record.seq} size is {actual_size} bytes but the manifest "
                f"records {record.byte_count}"
            )
        actual_hash = sha256_file(path)
        if actual_hash != record.sha256:
            report.checksum_mismatches.append(record.filename)
            report.problems.append(
                f"chunk {record.seq} checksum mismatch: file hashes to "
                f"{actual_hash[:12]}... but the manifest records "
                f"{record.sha256[:12]}... (the audio was modified or corrupted)"
            )
            continue
        if check_headers:
            problem = _verify_wav_header(path, record)
            if problem:
                report.header_mismatches.append(record.filename)
                report.problems.append(problem)
                continue
        report.verified_chunks += 1
        report.total_frames += record.frame_count
        report.total_bytes += record.byte_count

    if records:
        report.duration_seconds = report.total_frames / max(records[0].sample_rate, 1)

    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            continue
        if entry.name in known_files:
            continue
        if entry.name.endswith((PARTIAL_SUFFIX, CHUNK_META_SUFFIX, ".tmp")):
            report.unexpected_files.append(entry.name)
            report.problems.append(
                f"unfinalised artefact present: {entry.name} (run recovery)"
            )
        else:
            report.unexpected_files.append(entry.name)

    return report


def _verify_wav_header(path: Path, record: ChunkRecord) -> str | None:
    """Check a WAV file's header against the manifest record."""
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.getnframes()
    except (wave.Error, OSError, EOFError) as exc:
        return f"chunk {record.seq} is not a readable WAV file: {exc}"

    expected_width = SampleFormat.INT16.bytes_per_sample
    problems: list[str] = []
    if channels != record.channels:
        problems.append(f"channels {channels} != {record.channels}")
    if width != expected_width:
        problems.append(f"sample width {width * 8} bits != 16 bits")
    if rate != record.sample_rate:
        problems.append(f"sample rate {rate} != {record.sample_rate}")
    if frames != record.frame_count:
        problems.append(f"frame count {frames} != {record.frame_count}")
    if problems:
        return f"chunk {record.seq} WAV header disagrees with the manifest: " + ", ".join(
            problems
        )
    return None


def summarise_records(records: Iterable[ChunkRecord]) -> dict[str, Any]:
    """Aggregate chunk records for status reporting."""
    items = list(records)
    usable = [r for r in items if r.is_usable_audio]
    return {
        "chunk_count": len(items),
        "usable_chunk_count": len(usable),
        "total_frames": sum(r.frame_count for r in usable),
        "total_bytes": sum(r.byte_count for r in usable),
        "dropped_frames": sum(r.dropped_frames for r in items),
        "xrun_callbacks": sum(r.xrun_callbacks for r in items),
        "recovered_chunks": sum(
            1 for r in items if r.recovery_status == RecoveryStatus.RECOVERED.value
        ),
        "chain_sha256": compute_chain_hash(items) if items else "",
    }
