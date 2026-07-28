"""Crash recovery for interrupted recordings.

If the process is killed mid-meeting -- power loss, a forced termination, a
crash -- the audio already finalised is safe by construction (see
:mod:`mom_igd.audio.writer`), and one partial file may be left behind. This module
turns that partial into valid audio, or quarantines it if it cannot.

Non-negotiable rules:

* **Never overwrite a valid final chunk.** If a ``.wav`` already exists for a
  sequence, the partial is quarantined as evidence rather than allowed to replace
  audio that has already been hashed and verified.
* **Never delete evidence silently.** Anything ambiguous or corrupt moves to
  ``quarantine/`` with a reason recorded, so it can be inspected later.
* **Recover only whole frames.** A trailing fragment that does not complete a
  frame is discarded, and the exact number of bytes discarded is recorded.
* **Idempotent.** Running recovery twice is a no-op the second time.
* **Honest accounting.** Recovered frames, discarded bytes and unrecoverable
  chunks all appear in the report, the manifest and the audit trail. No silence is
  ever fabricated to disguise a gap.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from mom_igd.audio.backend import CaptureProfile, SampleFormat
from mom_igd.audio.manifest import (
    CHUNK_META_SUFFIX,
    MANIFEST_FILENAME,
    MANIFEST_SUMMARY_FILENAME,
    PARTIAL_SUFFIX,
    QUARANTINE_DIRNAME,
    ChunkRecord,
    ChunkStatus,
    ManifestWriter,
    RecoveryStatus,
    chunk_filename,
    read_manifest,
    sha256_file,
    utc_now_iso,
)
from mom_igd.audio.writer import build_wav_from_pcm, partial_meta_path, read_partial_meta
from mom_igd.logging_setup import get_logger

__all__ = [
    "RecoveredChunk",
    "RecoveryReport",
    "find_partials",
    "quarantine_file",
    "recover_recording",
    "scan_recoverable",
]

_LOG = get_logger("audio.recovery")
_TMP_SUFFIX: Final[str] = ".wav.recovering"


@dataclass(frozen=True, slots=True)
class RecoveredChunk:
    """One partial file, after recovery was attempted."""

    seq: int
    outcome: str
    frames_recovered: int = 0
    bytes_recovered: int = 0
    trailing_bytes_discarded: int = 0
    filename: str | None = None
    sha256: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "outcome": self.outcome,
            "frames_recovered": self.frames_recovered,
            "bytes_recovered": self.bytes_recovered,
            "trailing_bytes_discarded": self.trailing_bytes_discarded,
            "filename": self.filename,
            "sha256": self.sha256,
            "reason": self.reason,
        }


@dataclass(slots=True)
class RecoveryReport:
    """Outcome of recovering one recording directory."""

    directory: str
    partials_found: int = 0
    chunks_recovered: int = 0
    chunks_quarantined: int = 0
    frames_recovered: int = 0
    bytes_discarded: int = 0
    already_final: int = 0
    chunks: list[RecoveredChunk] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def changed(self) -> bool:
        return self.chunks_recovered > 0 or self.chunks_quarantined > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "ok": self.ok,
            "changed": self.changed,
            "partials_found": self.partials_found,
            "chunks_recovered": self.chunks_recovered,
            "chunks_quarantined": self.chunks_quarantined,
            "already_final": self.already_final,
            "frames_recovered": self.frames_recovered,
            "bytes_discarded": self.bytes_discarded,
            "chunks": [c.to_dict() for c in self.chunks],
            "problems": list(self.problems),
        }


def find_partials(directory: Path) -> list[tuple[int, Path]]:
    """Return ``(seq, path)`` for every partial chunk, lowest sequence first."""
    if not directory.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in sorted(directory.glob(f"*{PARTIAL_SUFFIX}")):
        stem = path.name.removesuffix(PARTIAL_SUFFIX)
        _, _, digits = stem.rpartition("_")
        try:
            found.append((int(digits), path))
        except ValueError:
            _LOG.warning("Ignoring partial with an unparseable name: %s", path.name)
    return sorted(found)


def scan_recoverable(recordings_root: Path) -> list[Path]:
    """Find recording directories that need recovery.

    A directory qualifies when it holds a partial chunk, an unfinished temporary
    WAV, or a manifest with no summary -- all signs the recording never completed
    its finalisation.
    """
    if not recordings_root.is_dir():
        return []
    candidates: list[Path] = []
    for manifest_path in sorted(recordings_root.rglob(MANIFEST_FILENAME)):
        directory = manifest_path.parent
        has_partial = bool(list(directory.glob(f"*{PARTIAL_SUFFIX}")))
        has_temp = bool(list(directory.glob("*.tmp"))) or bool(
            list(directory.glob(f"*{_TMP_SUFFIX}"))
        )
        no_summary = not (directory / MANIFEST_SUMMARY_FILENAME).is_file()
        if has_partial or has_temp or no_summary:
            candidates.append(directory)
    # A directory may hold partials without ever having written a manifest line.
    for partial in sorted(recordings_root.rglob(f"*{PARTIAL_SUFFIX}")):
        if partial.parent not in candidates:
            candidates.append(partial.parent)
    return candidates


def quarantine_file(path: Path, reason: str) -> Path:
    """Move a file into ``quarantine/`` instead of deleting it.

    Evidence of a failure is worth more than a tidy directory: it is the only way
    to explain afterwards why part of a meeting is missing.
    """
    target_dir = path.parent / QUARANTINE_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    counter = 1
    while target.exists():
        target = target_dir / f"{path.stem}.{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))
    note = target.with_suffix(target.suffix + ".reason.txt")
    note.write_text(f"{utc_now_iso()}  {reason}\n", encoding="utf-8")
    _LOG.warning("Quarantined %s: %s", path.name, reason)
    return target


def recover_recording(
    directory: Path, *, profile: CaptureProfile | None = None
) -> RecoveryReport:
    """Recover one recording directory. Safe to call repeatedly.

    Args:
        directory: The ``<meeting_uuid>/<recording_uuid>`` directory.
        profile: Fallback format, used only when a partial's metadata sidecar is
            missing. Without either, the partial cannot be interpreted and is
            quarantined rather than guessed at.
    """
    report = RecoveryReport(directory=directory.name)
    if not directory.is_dir():
        report.problems.append(f"recording directory does not exist: {directory}")
        return report

    existing_records, _, torn = read_manifest(directory)
    known_seqs = {record.seq for record in existing_records}
    if torn:
        _LOG.warning(
            "%s: %d torn manifest line(s) ignored (expected after a crash).",
            directory.name,
            torn,
        )

    manifest = ManifestWriter(directory)
    partials = find_partials(directory)
    report.partials_found = len(partials)

    # Remove abandoned temporary WAVs: they were never renamed into place, so by
    # construction they are incomplete and the partial is the real source.
    for stale in list(directory.glob("*.tmp")) + list(directory.glob(f"*{_TMP_SUFFIX}")):
        quarantine_file(stale, "temporary WAV left by an interrupted finalisation")
        report.chunks_quarantined += 1

    for seq, partial in partials:
        final_path = directory / chunk_filename(seq)
        if final_path.is_file():
            # The chunk was finalised; the partial simply outlived step 8.
            if seq in known_seqs:
                report.already_final += 1
                _cleanup_partial(directory, seq, partial)
                report.chunks.append(
                    RecoveredChunk(
                        seq=seq,
                        outcome="already_final",
                        reason="final chunk already exists and is recorded; partial removed",
                    )
                )
            else:
                quarantine_file(
                    partial,
                    f"final chunk {final_path.name} exists but is not in the manifest; "
                    "refusing to overwrite verified audio",
                )
                report.chunks_quarantined += 1
                report.chunks.append(
                    RecoveredChunk(
                        seq=seq,
                        outcome="quarantined",
                        reason="final chunk exists but is absent from the manifest",
                    )
                )
            continue

        meta = read_partial_meta(directory, seq)
        if meta is None and profile is None:
            quarantine_file(
                partial,
                "recovery metadata is missing and no fallback format is known, so "
                "the sample rate, channel count and format cannot be determined",
            )
            report.chunks_quarantined += 1
            report.chunks.append(
                RecoveredChunk(
                    seq=seq, outcome="quarantined", reason="format metadata unavailable"
                )
            )
            continue

        if meta is not None:
            channels = int(meta["channels"])
            sample_rate = int(meta["sample_rate"])
            sample_width = int(meta.get("bytes_per_frame", 0)) // max(channels, 1) or 2
            start_frame = int(meta.get("start_frame", 0))
            utc_start = str(meta.get("utc_start", utc_now_iso()))
            monotonic_start = int(meta.get("monotonic_start_ns", 0))
        else:
            assert profile is not None  # noqa: S101 - guarded above
            channels = profile.channels
            sample_rate = profile.sample_rate
            sample_width = profile.sample_format.bytes_per_sample
            start_frame = 0
            utc_start = utc_now_iso()
            monotonic_start = 0

        size = partial.stat().st_size
        bytes_per_frame = channels * sample_width
        if bytes_per_frame <= 0 or size < bytes_per_frame:
            quarantine_file(
                partial,
                f"partial holds {size} byte(s), less than one complete "
                f"{bytes_per_frame}-byte frame: there is no audio to recover",
            )
            report.chunks_quarantined += 1
            report.chunks.append(
                RecoveredChunk(
                    seq=seq,
                    outcome="quarantined",
                    reason="fewer bytes than one complete frame",
                )
            )
            continue

        temp_path = directory / (chunk_filename(seq).removesuffix(".wav") + _TMP_SUFFIX)
        try:
            frames, trailing = build_wav_from_pcm(
                partial,
                temp_path,
                channels=channels,
                sample_width=sample_width,
                sample_rate=sample_rate,
            )
        except (OSError, Exception) as exc:  # noqa: BLE001 - any failure is quarantine
            temp_path.unlink(missing_ok=True)
            quarantine_file(partial, f"could not rebuild a WAV from the partial: {exc}")
            report.chunks_quarantined += 1
            report.problems.append(f"chunk {seq} could not be recovered: {exc}")
            report.chunks.append(
                RecoveredChunk(seq=seq, outcome="quarantined", reason=str(exc))
            )
            continue

        digest = sha256_file(temp_path)
        byte_count = temp_path.stat().st_size
        import os as _os

        _os.replace(temp_path, final_path)

        record = ChunkRecord(
            seq=seq,
            filename=final_path.name,
            start_frame=start_frame,
            end_frame=start_frame + frames,
            frame_count=frames,
            duration_ms=round(frames * 1000.0 / sample_rate, 3),
            utc_start=utc_start,
            utc_end=utc_now_iso(),
            monotonic_start_ns=monotonic_start,
            monotonic_end_ns=monotonic_start,
            sample_rate=sample_rate,
            channels=channels,
            sample_format=SampleFormat.INT16.value,
            byte_count=byte_count,
            sha256=digest,
            status=ChunkStatus.TRUNCATED.value if trailing else ChunkStatus.WRITTEN.value,
            recovery_status=RecoveryStatus.RECOVERED.value,
            finalized=True,
            notes=(
                f"recovered from {partial.name}; "
                f"{trailing} trailing byte(s) discarded as an incomplete frame"
                if trailing
                else f"recovered from {partial.name}"
            ),
        )
        manifest.append_chunk(record)
        manifest.append_event(
            "recovery",
            seq=seq,
            frames_recovered=frames,
            trailing_bytes_discarded=trailing,
            sha256=digest,
        )
        _cleanup_partial(directory, seq, partial)

        report.chunks_recovered += 1
        report.frames_recovered += frames
        report.bytes_discarded += trailing
        report.chunks.append(
            RecoveredChunk(
                seq=seq,
                outcome="recovered",
                frames_recovered=frames,
                bytes_recovered=byte_count,
                trailing_bytes_discarded=trailing,
                filename=final_path.name,
                sha256=digest,
            )
        )
        _LOG.info(
            "Recovered chunk %d: %d frames, %d trailing byte(s) discarded.",
            seq,
            frames,
            trailing,
        )

    # Orphaned metadata sidecars whose partial is gone carry no audio.
    for meta_path in sorted(directory.glob(f"*{CHUNK_META_SUFFIX}")):
        stem = meta_path.name.removesuffix(CHUNK_META_SUFFIX)
        if not (directory / f"{stem}{PARTIAL_SUFFIX}").exists():
            meta_path.unlink(missing_ok=True)

    return report


def _cleanup_partial(directory: Path, seq: int, partial: Path) -> None:
    partial.unlink(missing_ok=True)
    partial_meta_path(directory, seq).unlink(missing_ok=True)
