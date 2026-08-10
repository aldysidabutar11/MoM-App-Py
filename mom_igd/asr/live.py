"""Live preview transcription: words on screen a few seconds after they are spoken.

**This is a preview and nothing else.** It is not the transcript, it is never stored as
one, and no minute is ever built from it. The authoritative transcript is still produced
after the recording by the two-pass pipeline, from the untouched master audio, with voice
activity detection, region batching, a budgeted second pass and terminology
normalisation. Everything here trades accuracy for latency, and the trade is only
acceptable because the result is thrown away.

Why it is worth having anyway: an operator watching a silent screen for ninety minutes
has no idea whether the machine is hearing anything. Text appearing as people speak is
the only honest confirmation that capture is working -- and this project has just spent a
day discovering that a muted microphone looks exactly like a working one.

WHAT PROTECTS THE RECORDING

* The audio arrives through :meth:`CaptureSession._feed_live_tap`, which runs after the
  master has been written and outside the writer's lock.
* :meth:`LiveTranscriber.feed` is non-blocking and **drops** audio it cannot keep up
  with. Preview text degrades; the master never does, because it is already on disk.
* Decoding happens on a worker thread that owns the model. It never touches the capture
  path, the writer, the manifest or the database.
* Every failure is contained. A model that will not load, a decode that raises, a queue
  that overflows -- each leaves the preview empty or stale and the recording untouched.

WHY THE MODEL IS IN THIS PROCESS AND NOT A SPAWNED WORKER

ADR-0004 puts each heavy model in a short-lived worker that exits. A live preview cannot
be short-lived: it runs for the whole meeting. Spawning a process and streaming audio to
it over a pipe would add a second failure mode -- a dead child mid-meeting -- to protect
against a memory profile that is already known: the pass-1 model is 693 MiB, and while a
recording is running nothing else heavy may start. So the preview holds the small model
in-process for the duration of the capture and releases it at stop. `asr transcribe` and
`mom generate` are both refused while a capture is live, so this cannot coincide with
either.
"""

from __future__ import annotations

import array
import queue
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final

from mom_igd.logging_setup import get_logger

__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "LiveSegment",
    "LiveTranscriber",
    "LiveTranscriptState",
]

_LOG = get_logger("asr.live")

#: How much speech is gathered before it is decoded, per profile.
#:
#: **Whisper pads every window to thirty seconds internally, so decode cost is nearly
#: flat in the window length.** Measured on the large model: 7.67 s for a 6-second window
#: and 8.16 s for a 12-second one. That single fact decides everything below, and missing
#: it produced a wrong conclusion first time round -- measuring only at 4 s and 6 s made
#: the accurate model look impossible (RTF 1.28) when it is merely impossible *at those
#: window lengths*.
#:
#:     model             window   decode    RTF
#:     small                 6s    1.73s   0.29
#:     large-v3-turbo        6s    7.67s   1.28   falls behind for ever
#:     large-v3-turbo       12s    8.16s   0.68   keeps up
#:     large-v3-turbo       20s    7.96s   0.40
#:
#: So the two uses get opposite settings, because they want opposite things. A voice
#: check answers "are my words coming out right?", where waiting twelve seconds for a
#: correct sentence beats six seconds of "pengenal" transcribed as "ponel". A preview
#: running beside a ninety-minute recording answers "is it still hearing me?", where
#: responsiveness is the whole point and the transcript is produced properly afterwards.
DEFAULT_WINDOW_SECONDS: Final[float] = 6.0

#: The accurate profile: the pass-2 model at a window long enough to carry it.
ACCURATE_WINDOW_SECONDS: Final[float] = 12.0
ACCURATE_ROLE: Final[str] = "pass2"
FAST_ROLE: Final[str] = "pass1"

#: Audio kept from the previous window, prepended to the next one. Speech does not stop
#: at a four-second boundary, and a word cut in half is transcribed as two wrong words.
DEFAULT_OVERLAP_SECONDS: Final[float] = 0.6

#: The preview queue holds this much audio, **measured in bytes, not in slots**.
#:
#: Sizing it by slot count was wrong and failed loudly: the slot count was derived by
#: assuming 8 KiB blocks, the driver delivers much smaller ones, and the real capacity
#: came out at a few seconds rather than twelve. The accurate profile then dropped **246
#: blocks in a thirty-second check** -- it spends eight seconds inside one decode, during
#: which nothing is consumed, and the queue overflowed every time.
#:
#: The floor below is derived from the window and the decode instead of guessed, so a
#: slower model or a longer window cannot silently reintroduce the same overflow.
DEFAULT_QUEUE_SECONDS: Final[float] = 12.0

#: Headroom over one window for the decode itself. The accurate profile measured 8.2 s
#: for a 12-second window; twelve gives room for a slow one without letting the preview
#: fall a minute behind and still call itself live.
_DECODE_HEADROOM_SECONDS: Final[float] = 12.0

#: Below this RMS the window is not sent to the decoder at all.
#: Whisper hallucinates fluently on silence, and a preview that invents sentences during
#: a pause is worse than a preview that shows nothing.
SILENCE_RMS_DBFS: Final[float] = -55.0

#: Above this, the decoder's own ``no_speech_prob`` means it does not believe the window
#: contained speech, and its text is discarded.
#:
#: A level gate alone is not enough, and finding that out cost nothing but was
#: instructive: a **loud** room with nobody talking measured -33 dBFS, sailed past the
#: silence threshold, and produced "Terima kasih" in five consecutive windows. Loudness
#: is not speech, and the only component that can tell the difference is the model.
MAX_NO_SPEECH_PROB: Final[float] = 0.6

#: Below this average token log-probability the decoder was guessing.
#:
#: -1.0, the same figure Whisper's own fallback heuristic uses and the same one pass-2
#: selection uses. It was briefly -0.9, on the reasoning that a preview has no second
#: pass to correct a bad window -- and that stricter bar silently swallowed the tail of a
#: real sentence during testing. Dropping words somebody actually said is the failure
#: this panel exists to catch, so the threshold matches the rest of the project rather
#: than being tightened on a hunch.
MIN_AVG_LOGPROB: Final[float] = -1.0

#: Phrases Whisper emits when handed audio that is not speech. Not a general profanity
#: or stopword list -- each one is a specific, observed artefact, and each is dropped
#: only when it is the *entire* window. "Terima kasih" is a real thing to say in a
#: meeting, so it survives whenever anything else was said with it.
_HALLUCINATION_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "terima kasih",
        "terima kasih.",
        "terima kasih telah menonton",
        "terima kasih telah menonton.",
        "sampai jumpa",
        "sampai jumpa di video selanjutnya",
        "silakan berlangganan",
        "subscribe",
        "thank you",
        "thank you.",
        "thanks for watching",
        "you",
        ".",
    }
)


@dataclass(frozen=True, slots=True)
class LiveSegment:
    """One decoded window of preview text."""

    text: str
    started_ms: int
    ended_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "started_ms": self.started_ms, "ended_ms": self.ended_ms}


@dataclass(slots=True)
class LiveTranscriptState:
    """What the interface shows. Counts included, because a preview must be honest."""

    running: bool = False
    model_ready: bool = False
    segments: list[LiveSegment] = field(default_factory=list)
    audio_seconds: float = 0.0
    decoded_windows: int = 0
    #: Windows the decoder produced text for that the filters rejected as invented.
    #: Reported, never hidden: an operator who speaks and sees nothing appear must be
    #: able to tell "the microphone is dead" from "that was not clear enough to trust".
    #: Without this the filters would recreate the exact confusion they were added after.
    filtered_windows: int = 0
    dropped_blocks: int = 0
    last_error: str | None = None

    @property
    def text(self) -> str:
        return " ".join(segment.text for segment in self.segments if segment.text)

    def to_dict(self, *, limit: int = 60) -> dict[str, Any]:
        recent = self.segments[-limit:]
        return {
            "running": self.running,
            "model_ready": self.model_ready,
            "segments": [segment.to_dict() for segment in recent],
            "text": " ".join(segment.text for segment in recent if segment.text),
            "audio_seconds": round(self.audio_seconds, 1),
            "decoded_windows": self.decoded_windows,
            "filtered_windows": self.filtered_windows,
            "dropped_blocks": self.dropped_blocks,
            "last_error": self.last_error,
            # Said in the payload itself, so no interface can present this as the
            # transcript by leaving a label off.
            "is_preview": True,
        }


class LiveTranscriber:
    """Decodes a running capture into preview text. Start, feed, read, stop."""

    def __init__(
        self,
        models_dir: Path,
        *,
        source_rate: int,
        source_channels: int,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
        queue_seconds: float = DEFAULT_QUEUE_SECONDS,
        cpu_threads: int = 4,
        language: str = "id",
        beam_size: int = 5,
        initial_prompt: str | None = None,
        role: str = FAST_ROLE,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        self._models_dir = Path(models_dir)
        self._source_rate = int(source_rate)
        self._source_channels = max(1, int(source_channels))
        self._window_seconds = max(1.0, float(window_seconds))
        self._overlap_seconds = max(0.0, float(overlap_seconds))
        # Fewer threads than the batch pipeline on purpose: this runs *during* a
        # recording, and the capture callback and writer must never wait for a core.
        self._cpu_threads = max(1, int(cpu_threads))
        self._language = language
        # Beam 5 rather than greedy. Measured at 1.69 s against 1.50 s on the same
        # window -- thirteen per cent, not the 2.5x the batch pipeline pays, because
        # these windows are short and emit few tokens. Free accuracy is taken.
        self._beam_size = max(1, int(beam_size))
        # The same terminology the batch pipeline primes with. Whisper spells unfamiliar
        # words by ear, and a meeting is full of them.
        self._initial_prompt = initial_prompt
        #: Which provisioned model to load. ``pass1`` is small and quick; ``pass2`` is
        #: large-v3-turbo and only keeps up at the longer window above.
        self._role = role
        self._on_update = on_update

        # Bounded by bytes, because the block size is the driver's choice and not
        # something this code may assume. The budget covers one whole window plus the
        # time it takes to decode it: while a decode runs, nothing is consumed.
        self._bytes_per_second = self._source_rate * self._source_channels * 2
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._queued_bytes = 0
        self._queue_budget = int(
            max(queue_seconds, self._window_seconds + _DECODE_HEADROOM_SECONDS)
            * self._bytes_per_second
        )
        self._state = LiveTranscriptState()
        self._last_text = ""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._provider: Any = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Begin decoding. Returns immediately; the model loads on the worker thread."""
        if self._thread is not None:
            return
        self._stop.clear()
        with self._lock:
            self._state = LiveTranscriptState(running=True)
        self._thread = threading.Thread(
            target=self._run, name="mom-igd-live-asr", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 8.0) -> LiveTranscriptState:
        """Stop decoding and release the model. Safe to call more than once."""
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                _LOG.warning(
                    "Live preview thread did not stop within %.1fs; it is a daemon and "
                    "will not hold the process open.",
                    timeout,
                )
        with self._lock:
            self._state.running = False
            return self._snapshot_locked()

    def feed(self, pcm: bytes) -> None:
        """Hand over one block of capture audio. **Non-blocking; drops when behind.**

        Called from the capture writer thread, so it must return immediately and must
        not raise. A full queue means the decoder is behind, and the right answer is to
        discard: the master audio already holds every byte, and stale preview text
        pretending to be live is worse than a gap in it.
        """
        if self._thread is None:
            return
        with self._lock:
            if self._queued_bytes + len(pcm) > self._queue_budget:
                self._state.dropped_blocks += 1
                return
            self._queued_bytes += len(pcm)
        self._queue.put_nowait(pcm)

    def snapshot(self) -> LiveTranscriptState:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> LiveTranscriptState:
        return LiveTranscriptState(
            running=self._state.running,
            model_ready=self._state.model_ready,
            segments=list(self._state.segments),
            audio_seconds=self._state.audio_seconds,
            decoded_windows=self._state.decoded_windows,
            filtered_windows=self._state.filtered_windows,
            dropped_blocks=self._state.dropped_blocks,
            last_error=self._state.last_error,
        )

    # -- the worker ----------------------------------------------------------

    def _run(self) -> None:
        try:
            self._load_model()
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the capture
            self._fail(f"{type(exc).__name__}: {exc}")
            return
        try:
            self._decode_loop()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            provider, self._provider = self._provider, None
            if provider is not None:
                try:
                    provider.close()
                except Exception:  # noqa: BLE001 - teardown must not mask a result
                    pass

    def _load_model(self) -> None:
        from mom_igd.asr.faster_whisper_provider import FasterWhisperProvider, resolve_model

        resolved = resolve_model(self._models_dir, role=self._role, deep=False)
        provider = FasterWhisperProvider(resolved, cpu_threads=self._cpu_threads)
        provider.load()
        self._provider = provider
        with self._lock:
            self._state.model_ready = True
        _LOG.info(
            "asr.live.model_loaded",
            extra={"threads": self._cpu_threads, "role": self._role},
        )

    def _decode_loop(self) -> None:
        from mom_igd.asr.normalize import WORKING_SAMPLE_RATE, downmix_to_mono, resample_linear

        window_samples = int(self._window_seconds * WORKING_SAMPLE_RATE)
        overlap_samples = int(self._overlap_seconds * WORKING_SAMPLE_RATE)
        pending = array.array("h")
        carry: int | None = None
        position = 0.0
        elapsed_ms = 0

        while not self._stop.is_set():
            try:
                block = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if block is None:
                break
            with self._lock:
                self._queued_bytes = max(0, self._queued_bytes - len(block))

            samples = array.array("h")
            samples.frombytes(block)
            mono = downmix_to_mono(samples, self._source_channels)
            resampled, carry, position = resample_linear(
                mono, self._source_rate, WORKING_SAMPLE_RATE, carry=carry, position=position
            )
            pending.extend(resampled)
            with self._lock:
                self._state.audio_seconds += len(mono) / self._source_rate

            while len(pending) >= window_samples and not self._stop.is_set():
                window = pending[:window_samples]
                # Keep a tail so the next window does not start mid-word.
                pending = pending[max(0, window_samples - overlap_samples) :]
                started = elapsed_ms
                elapsed_ms += int(
                    (window_samples - overlap_samples) * 1000 / WORKING_SAMPLE_RATE
                )
                self._decode_window(window, started, elapsed_ms)

    def _decode_window(self, window: array.array, started_ms: int, ended_ms: int) -> None:
        if _rms_dbfs(window) < SILENCE_RMS_DBFS:
            # Silence is not sent to the decoder. Whisper fills a silent window with
            # fluent invented speech, and a preview that does that during a pause
            # teaches the operator to distrust the one thing confirming capture works.
            return
        provider = self._provider
        if provider is None:
            return
        try:
            # `transcribe_preview`, never `transcribe`: the batch method carries the
            # evidence chain, and the preview must not share a code path with it.
            text, no_speech, logprob = provider.transcribe_preview(
                window,
                language=self._language,
                beam_size=self._beam_size,
                initial_prompt=self._initial_prompt,
            )
        except Exception as exc:  # noqa: BLE001 - one bad window must not end the preview
            _LOG.debug("asr.live.decode_failed", extra={"reason": type(exc).__name__})
            with self._lock:
                self._state.last_error = type(exc).__name__
            return

        raw_had_text = bool(text.strip())
        text = _drop_if_invented(
            text, no_speech=no_speech, logprob=logprob, previous=self._last_text
        )
        # Windows overlap so a word is not cut in half, which means the words in the
        # overlap are decoded twice. Observed on screen as "...suara otomatis" followed
        # by "otomatis berbasis...". The repeat is removed here rather than left for a
        # reader to mentally skip.
        text = _strip_repeated_prefix(text, self._last_text)
        if text:
            self._last_text = text
        elif raw_had_text:
            with self._lock:
                self._state.filtered_windows += 1
        with self._lock:
            self._state.decoded_windows += 1
            if text:
                self._state.segments.append(
                    LiveSegment(text=text, started_ms=started_ms, ended_ms=ended_ms)
                )
        if text and self._on_update is not None:
            try:
                self._on_update()
            except Exception:  # noqa: BLE001 - a listener must not break the preview
                pass

    def _fail(self, message: str) -> None:
        _LOG.warning("asr.live.failed", extra={"reason": message})
        with self._lock:
            self._state.last_error = message
            self._state.running = False


def _drop_if_invented(
    text: str, *, no_speech: float, logprob: float, previous: str
) -> str:
    """Return the text, or an empty string when it should not be shown.

    Four filters, in the order they cost. Every one of them exists because the operator
    is reading this to decide whether the microphone is capturing their meeting
    correctly -- an invented sentence does not merely look untidy, it answers that
    question wrongly, and it answers it confidently.

    The decoder's own doubt is the strongest signal and the model is the only component
    that has it. Repetition is the second: Whisper's failure mode on non-speech is a
    loop, and the same phrase arriving in consecutive four-second windows is far more
    likely to be that loop than two people saying the identical thing twice running.
    """
    body = text.strip()
    if not body:
        return ""
    if no_speech > MAX_NO_SPEECH_PROB:
        return ""
    if logprob < MIN_AVG_LOGPROB:
        return ""
    if _is_only_filler(body):
        return ""
    if previous and body.casefold() == previous.casefold():
        return ""
    return body


def _strip_repeated_prefix(text: str, previous: str) -> str:
    """Drop leading words that already ended the previous window.

    The overlap exists so a word spoken across a window boundary is not cut in half; the
    cost is that those seconds are decoded twice and the shared words appear twice. Up to
    six words are considered, which is more than the overlap can hold at any sane speech
    rate, and the longest match wins.

    Matching is on casefolded words with punctuation stripped, because the two decodes of
    the same audio routinely differ in exactly that -- one ends "otomatis" and the next
    begins "Otomatis,".
    """
    if not text or not previous:
        return text
    new_words = text.split()
    old_words = previous.split()
    if not new_words or not old_words:
        return text

    def key(word: str) -> str:
        return word.casefold().strip(".,!?;:")

    for length in range(min(6, len(new_words), len(old_words)), 0, -1):
        head = [key(word) for word in new_words[:length]]
        tail = [key(word) for word in old_words[-length:]]
        if head == tail:
            remainder = " ".join(new_words[length:]).strip()
            # An entire window that merely repeats the previous one carries nothing new.
            return remainder
    return text


def _is_only_filler(body: str) -> bool:
    """True when a window contains nothing but known artefacts.

    Checked sentence by sentence rather than against the whole string. The first version
    compared the entire window to the phrase list, so a bare "Terima kasih." was caught
    and **"Terima kasih. Terima kasih."** was not -- which is exactly what reached the
    screen. Whisper's artefact on non-speech is usually the same phrase repeated, so the
    repeated form is the common case and the single one is the exception.

    A window is dropped only when *every* sentence in it is an artefact. "Terima kasih
    pak Andi atas laporannya" keeps all of itself, because something real was said.
    """
    bare = {phrase.strip(" .!?").casefold() for phrase in _HALLUCINATION_PHRASES}
    sentences = [
        part.strip(" .!?").casefold()
        for part in re.split(r"[.!?]+", body)
        if part.strip(" .!?")
    ]
    return bool(sentences) and all(sentence in bare for sentence in sentences)


def _rms_dbfs(samples: array.array) -> float:
    """RMS in dBFS. Pure Python over `array`, so no NumPy on the capture-adjacent path."""
    if not samples:
        return -999.0
    total = 0
    for value in samples:
        total += value * value
    mean = total / len(samples)
    if mean <= 0:
        return -999.0
    import math

    return 10 * math.log10(mean / (32768.0 * 32768.0))


def decode_once(
    models_dir: Path,
    pcm: bytes,
    *,
    source_rate: int,
    source_channels: int,
    language: str = "id",
    initial_prompt: str | None = None,
    cpu_threads: int = 6,
    role: str = ACCURATE_ROLE,
) -> tuple[str, str | None]:
    """Decode a complete, already-captured clip in **one** pass.

    The counterpart to `LiveTranscriber`, and deliberately not the same thing. A live
    preview has to chunk, and chunking costs accuracy at every seam: a fixed window cuts
    words in half, the overlap that prevents that decodes the same seconds twice, and each
    fragment reaches the model without the sentence around it. Measured on a real check,
    six-second windows of the small model turned "fitur pengenal suara" into "fitur ponel
    suara" and lost "hari Senin" entirely.

    A voice check has no reason to accept any of that, because by the time it decodes, the
    audio is complete and in memory. So it goes to the accurate model whole, with the
    sentence for context. Whisper pads every window to thirty seconds internally, so
    decoding thirty costs about what decoding six does -- roughly eight seconds either
    way. The accuracy is nearly free; only the wait is real, and it lands after the
    operator has stopped talking.

    Returns ``(text, reason)``. An empty text **always** carries a reason. The ways this
    can produce nothing are different facts -- no audio arrived, the clip was too short,
    the model judged it silence, the model returned nothing -- and a panel that renders
    all of them as one blank box teaches the operator that the feature is broken. That is
    the exact confusion this whole path exists to remove, so it must not be recreated at
    the end of it.

    Never raises: the caller has a streaming preview that still stands.
    """
    if not pcm:
        return ("", "NO_AUDIO")
    try:
        from mom_igd.asr.faster_whisper_provider import FasterWhisperProvider, resolve_model
        from mom_igd.asr.normalize import (
            WORKING_SAMPLE_RATE,
            downmix_to_mono,
            resample_linear,
        )

        raw = array.array("h")
        raw.frombytes(pcm)
        mono = downmix_to_mono(raw, source_channels)
        resampled, _carry, _position = resample_linear(
            mono, source_rate, WORKING_SAMPLE_RATE
        )
        if len(resampled) < WORKING_SAMPLE_RATE:
            return ("", "TOO_SHORT")

        resolved = resolve_model(models_dir, role=role, deep=False)
        provider = FasterWhisperProvider(resolved, cpu_threads=cpu_threads)
        try:
            provider.load()
            text, no_speech, _logprob = provider.transcribe_preview(
                resampled,
                language=language,
                beam_size=5,
                initial_prompt=initial_prompt,
            )
        finally:
            provider.close()

        # The model's own verdict still applies. A fluent sentence over a room with
        # nobody in it is precisely the failure this panel exists to avoid presenting
        # as fact, and one accurate pass is not exempt from that.
        if no_speech > MAX_NO_SPEECH_PROB:
            return ("", "NO_SPEECH")
        if not text.strip():
            # It ran, it was not confident the clip was silence, and it still emitted
            # nothing. Rare, and worth separating from the silence verdict: it points at
            # the audio rather than at the room.
            return ("", "EMPTY_DECODE")
        return (text, None)
    except Exception as exc:  # noqa: BLE001 - the streaming preview still stands
        _LOG.warning(
            "asr.live.single_pass_failed",
            extra={"reason": type(exc).__name__},
        )
        return ("", type(exc).__name__)
