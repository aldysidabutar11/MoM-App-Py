# ADR-0017 — The minutes engine: llama.cpp, a 4B GGUF, and a grammar

* **Status:** Accepted
* **Relates to:** ADR-0002 (offline runtime), ADR-0004 (one heavy worker), ADR-0014
  (ASR provider), ADR-0015 (model provisioning), ADR-0018 (minutes pipeline shape)

## Context

Turning a transcript into minutes needs a language model, and this application may not
reach a network at runtime. So the model runs locally, on a CPU, next to a desktop the
operator is also using, on a machine with 16 GB of RAM and an Intel Iris Xe that no
inference stack here can use.

Three questions had to be answered: which runtime, which model, and how the output is kept
to a shape the rest of the code can rely on.

## Decision

### 1. llama.cpp (`llama-cpp-python`), not the CTranslate2 stack already present

CTranslate2 is already a dependency for transcription, which made it the obvious candidate
and the wrong one. CTranslate2 cannot load a distributed language model directly: it needs
a **conversion step**, and the converter requires `transformers` **and** `torch`. Both are
multi-gigabyte, both are on the deferred list, and `torch` in particular is the dependency
this project has worked hardest to avoid. Adding two gigabytes of build-time dependency to
avoid adding six megabytes of runtime dependency is the wrong trade.

GGUF is distributed ready to run. The wheel is prebuilt for cp312/win_amd64, so no C++
toolchain is installed and nothing is compiled here.

**It is not on PyPI for this platform.** `pip install llama-cpp-python` reports
`from versions: none`, and with a source fallback would demand MSVC. The wheel comes from
the maintainer's own CPU index:

```powershell
.venv\Scripts\python.exe -m pip install llama-cpp-python==0.3.34 `
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

That index is CPU-only by construction. The `cuXXX` variants must never be used: the target
GPU is Intel Iris Xe and there is no CUDA on the production device.

### 2. Qwen3-4B-Q4_K_M, from Qwen's own repository

Four billion parameters at 4-bit is the size that fits. The requirement is Indonesian with
English technical terms mixed in, which rules out the English-first small models, and the
generation budget is minutes rather than hours, which rules out anything larger on a CPU.

Taken from **`Qwen/Qwen3-4B-GGUF`**, the first-party repository, rather than one of the
several community re-quantisations that score similarly. Provenance is the deciding factor
when the artefact will be hash-pinned and trusted: 2.33 GB, Apache-2.0, revision
`bc640142c66e`, one file, `Qwen3-4B-Q4_K_M.gguf`.

It is provisioned by the **same** command, catalogue, manifest and load-probe machinery as
the ASR models (ADR-0015), extended rather than duplicated: `ModelSpec` gained a `kind`
field, the slot map gained `llm`, and `_probe_promoted_llm` runs a load-and-generate probe
in an isolated worker. Byte verification remains necessary and not sufficient — a GGUF can
hash perfectly and still use a quantisation the installed build cannot read, and only a
probe that produces a token proves otherwise. The probe passed in 3.89 s.

A missing model is `MODEL_UNAVAILABLE`. Never a download, never a substitution.

### 3. Structure is enforced by a GBNF grammar, not requested in the prompt

A model told to "reply with JSON" complies most of the time. The remainder needs a repair
step, and a repair step is where a truncated object quietly becomes a shorter list and a
mangled field becomes an invented one. llama.cpp constrains sampling to a grammar, so the
tokens that would produce malformed output are never sampled at all. Malformed output is
not unlikely; it is unreachable. That is what makes it safe to parse the result directly.

Three things were learned by measurement rather than by reading:

* **`LlamaGrammar.from_string` does not parse anything.** It stores the string and returns.
  An invalid grammar therefore travels to the sampler, where llama.cpp prints
  `parse: error parsing grammar` to stderr and returns a null pointer, and the first thing
  Python sees is an `OSError` with no indication that a grammar was involved.
  `LocalLlm.validate_grammar` now builds the grammar against the real vocabulary and frees
  it immediately, turning that into a message. It cost an hour to find the first time.
* **GBNF ends a rule at the newline.** A rule wrapped across lines for readability is a
  parse error. A test asserts every line of every grammar contains `::=`.
* **Grammar sampling costs about 2.4× on generation** — 14.6 tokens a second
  unconstrained against roughly 6 constrained — because llama.cpp checks every candidate
  token of a 151 000-token vocabulary against the grammar stacks on every step. This is
  paid deliberately. String lengths are built from sixteen-character blocks rather than a
  flat `c{0,400}`, which accepts exactly the same strings with an automaton about forty
  states deep instead of four hundred.

Thinking is not disabled by a flag; the grammar makes it unreachable. Qwen3 is a hybrid
reasoning model and emits `<think>` when unconstrained — measured at 13.8 s for 200 tokens
of deliberation about a trivial question. Under the extraction grammar the first sampled
token must open the JSON object, so no reasoning tokens can be produced.

## Consequences

* **Peak worker memory is about 5.1 GB and cannot be brought under the 2.5 GB heavy-worker
  budget.** See ADR-0018 §4 for the measured breakdown and what is claimed instead.
* Generation runs at roughly 6–7 tokens a second on twelve threads. A ninety-minute meeting
  is about nine windows and projects to fifteen to twenty minutes.
* The model loads in 2.3 s from a memory-mapped file, so a short-lived worker per stage
  costs almost nothing and the one-heavy-worker policy is kept without argument.
* `llama-cpp-python` graduates out of `DEFERRED_HEAVY_DISTRIBUTIONS`, along with its two
  declared dependencies `diskcache` and `jinja2`. `transformers` and
  `sentence-transformers` stay deferred: GGUF needs neither, and either would pull `torch`.
* `jinja2` renders the chat template **embedded in the GGUF file**. It is a local string;
  nothing loads a template over a network.

## Alternatives rejected

**CTranslate2 with a converted model.** Reuses the engine already present, and requires
`transformers` plus `torch` to do the conversion. Rejected on dependency weight.

**A 7B or 8B model.** Better extraction, and 4.5 GB of weights plus a repacked copy on a
16 GB machine that is also running Word — with generation roughly halving. The operator
asked for 4B and the measured 4B output is good: on realistic Indonesian meeting text it
produced six items, all verified, with both named owners and both stated deadlines correct
and no owner invented for the one action the meeting deliberately left unassigned.

**A 1.5B model.** Fits comfortably and hallucinates owners, which is the one failure this
system must not have.

**An OpenAI-compatible local server (llama-server, Ollama, LM Studio).** Would put an HTTP
listener and a separate process lifecycle into an application whose offline guarantee rests
on there being no server to talk to. The in-process library keeps the boundary where
ADR-0002 put it.

**Asking for JSON and repairing what comes back.** No new dependency, and it reintroduces
exactly the failure mode the grammar removes.
