"""Minutes extraction: a reviewed transcript in, a structured, evidenced minute out.

**Everything here is grounded or it does not exist.** Every decision, action item and open
question carries the segment ids and timestamps it came from, and a **non-LLM verifier**
checks that each quotation really appears in those segments before the item is stored. An
item that fails is flagged for review rather than silently kept, and a fabricated reference
is discarded outright. That verifier is the point of this package: the language model is a
proposer, and nothing it proposes is trusted on its own authority.

Constraints this package holds to, in the order they matter:

* **Nothing here downloads anything.** The model is provisioned once by
  ``asr provision mom-llm``, hash-verified and load-probed before it is recorded ready.
  Absent, it is ``MODEL_UNAVAILABLE`` -- never a fetch, never a fall back to a different
  model.
* **The model runs in a short-lived worker process** and never beside an ASR model. The
  weights are 2.3 GB against a 2.5 GB budget, so co-residency is not a preference.
* **Structure is enforced by a GBNF grammar**, not by asking the model to reply with JSON.
  Malformed output is not merely unlikely: the tokens are never sampled. That is what
  removes the need for a repair step, and a repair step is where an invented field gets in.
* **A minute is a draft until a human approves it.** Nothing here marks anything approved.
* **No speaker attribution.** Phase 4 assigns none and this package invents none: a PIC is
  recorded only when the transcript *names* one. "Whoever was speaking" is a guess, and a
  guess about who is responsible for what is the most damaging thing a minute can contain.
"""

from __future__ import annotations

__all__: list[str] = []
