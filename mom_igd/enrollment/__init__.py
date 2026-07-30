"""Phase 3: participant registry, biometric consent, and voice enrollment.

**What this package is.** Everything needed to register the people who attend a
meeting, record their explicit consent to biometric processing, and build one
encrypted voiceprint per person from a short microphone enrollment.

**What this package deliberately is not.** There is no speaker identification
here, and no matching of meeting audio to a name. Phase 3 *creates* templates;
Phase 6 compares them. There is also no VAD model, no ASR, no diarization and no
LLM: an embedding is computed by an external provider behind a narrow contract
(:mod:`mom_igd.enrollment.provider`), and no such provider has been approved yet.

**Layering.** Each module has one job, and the security-critical ones are kept
apart from the orchestration so they can be reasoned about on their own:

``keys``
    Windows DPAPI through ``ctypes``. Protects one random 256-bit master key.
``cipher``
    AES-256-GCM envelope. Versioned, with authenticated additional data binding
    each ciphertext to its participant, voiceprint and embedding model.
``store``
    Reads and writes envelopes on disk. Never returns a plaintext vector to a
    caller that only needs metadata.
``consent``
    The consent text, its version and hash, and the append-only event log.
``participants``
    Participant lifecycle and the nine-per-meeting cap.
``provider`` / ``fake_provider``
    The speaker-embedding boundary, and a deterministic stand-in for tests.
``quality``
    Enrollment quality gates, built on the Phase 2 meter.
``service``
    The enrollment state machine, and the only place that orchestrates the above.

**Invariants that must survive any future edit.**

* No embedding, ciphertext, key or raw enrollment audio ever reaches the
  database, a log, an API response or the audit trail.
* The master key is created only by an explicit enrollment. Importing this
  package, listing participants and running ``doctor`` must never create one.
* Enrollment audio is held in bounded memory and never written to disk.
* Enrollment reuses the Phase 2 capture path and its single-capture lock, so a
  meeting recording and an enrollment can never open the microphone together.
"""

from __future__ import annotations

__all__: list[str] = []
