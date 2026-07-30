"""Phase 4: offline automatic speech recognition.

**What this package is.** Everything needed to turn a finished Phase 2 recording into
a timestamped transcript, entirely on this machine: audio validation, a 16 kHz mono
working copy, voice activity detection, a first ASR pass, deterministic selection of
risky segments, a selective second pass, a merge, and deterministic terminology
normalisation.

**What this package deliberately is not.** There is no diarization, no speaker
separation, no voice identification, no speaker clustering, no overlap attribution and
no LLM. A Phase 4 transcript has **no speaker identity**: every segment carries
``speaker = None`` with ``speaker_status = "UNASSIGNED"``. Phase 5 assigns speakers;
guessing a name here would be worse than admitting we do not know.

**Layering.** The security- and correctness-critical parts are kept apart from the
orchestration so each can be reasoned about alone:

``manifest``
    The on-disk model manifest: every file, its size and its SHA-256. This is the
    hash chain that makes "the model is what we provisioned" checkable.
``provision``
    The **only** code that downloads anything, reachable **only** from an explicit
    command. Staging, verification, then atomic promotion.
``registry_entry``
    Turns a promoted model directory into a reviewable ``models/registry.json`` entry.
``provider``
    The production provider contract, plus hard validation of whatever a provider
    returns. Nothing downstream trusts a provider's output shape.
``faster_whisper_provider``
    The real provider. ``ctranslate2`` is imported **inside** the load call, so
    importing this package costs nothing.
``fake_provider``
    A deterministic stand-in for tests. It cannot be selected by configuration, an
    environment variable or a request -- only by constructor injection inside a test.

**Invariants that must survive any future edit.**

* **Nothing on a runtime path downloads a model.** Import, ``doctor``, the API, the
  shell and the pipeline all fail closed with ``MODEL_UNAVAILABLE`` instead.
* **A model is loaded from a local path whose manifest hash verifies**, never from a
  cache directory that "probably has it".
* **The Phase 2 master audio is never modified.** The working copy is derived, is
  regenerable, and records its provenance back to the master.
* **Exactly one heavy model is resident at a time**, in a short-lived worker process
  that exits so the operating system reclaims the memory.
* **Heavy work never runs while a recording is active.** The Phase 2 capture lock is
  the mechanism, not a convention.
* **No transcript text and no participant name is ever written to a log, an audit
  detail, an error message or a worker's stdout.**
"""

from __future__ import annotations

__all__: list[str] = []
