"""The local language model: loaded from a verified GGUF, offline, one at a time.

**Why llama.cpp and not CTranslate2**, which is already a dependency: CTranslate2 needs a
converted model, and converting one needs `transformers` plus `torch` — a multi-gigabyte
dependency this project has deliberately deferred to a phase that needs it. GGUF is
distributed ready to run. The wheel is prebuilt for this platform, so nothing is compiled
here and no C++ toolchain is installed (ADR-0017).

**Nothing in this module downloads anything.** It is handed a directory that provisioning
has already hashed, promoted and probed, exactly as the ASR provider is. A missing model is
`MODEL_UNAVAILABLE`, never a fetch.

**The structure of an answer is enforced by a grammar, not by asking nicely.** A model told
to "reply with JSON" will occasionally reply with prose, or with JSON wrapped in a code
fence, or with a trailing apology. llama.cpp constrains sampling to a GBNF grammar, so
malformed output is not merely unlikely — the tokens that would produce it are never
sampled. That is what makes it safe to parse the result without a repair step, and a repair
step is where a "helpful" fallback would quietly invent a field.

**No prose is generated that is not grounded.** This module supplies the mechanism; the
extraction rules and the verifier that checks every quotation against the transcript live in
`extract.py` and `verify.py`. The division matters: this file must not know what a decision
or an action item is, or it becomes the place where somebody adds "just infer the PIC".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from mom_igd.logging_setup import get_logger

__all__ = [
    "DEFAULT_BATCH_TOKENS",
    "DEFAULT_CONTEXT_TOKENS",
    "GenerationRequest",
    "GenerationResult",
    "LlmError",
    "LlmModelInfo",
    "LlmUnavailableError",
    "LocalLlm",
    "resolve_llm",
    "resolve_verified_llm_directory",
]

_LOG = get_logger("mom.llm")

#: Context window the extraction actually uses.
#:
#: Every token costs resident memory for a KV cache -- **measured at 1152 MiB for 8192,
#: 864 MiB for 6144 and 576 MiB for 4096** -- so this started at 6144 to save memory. That
#: was the wrong trade once the instructions grew, and the projection says why. A window
#: holds ``n_ctx`` minus a fixed 3448 tokens of reserve (2048 for the answer, 1400 for the
#: instructions), so shrinking the context attacks the *remainder*, not the total::
#:
#:     n_ctx   window   windows for a 90-minute meeting   projected wall time
#:      4096      648                                81            142 minutes
#:      6144     2696                                17             32 minutes
#:      8192     4744                                 9             18 minutes
#:
#: 8192 nearly halves the run for 288 MiB, on a machine measured with 8.1 GB free. 4096 is
#: catastrophic and is why :class:`mom_igd.config.MomConfig` will not accept it.
DEFAULT_CONTEXT_TOKENS: Final[int] = 8192

#: Batch size for prompt evaluation. **512 costs 563 MiB of compute buffer and 256 costs
#: 281 MiB, for no measurable difference in throughput** (7.11 against 7.18 tokens a
#: second over the same prompt). Free memory, so it is taken.
DEFAULT_BATCH_TOKENS: Final[int] = 256

#: Threads. Twelve is the measured optimum for the ASR models on this machine's twelve
#: physical cores, and llama.cpp is bound the same way. Re-measure on other hardware
#: rather than reasoning from core counts.
DEFAULT_THREADS: Final[int] = 12


class LlmError(RuntimeError):
    """The language model failed. The message never contains transcript text."""


class LlmUnavailableError(LlmError):
    """No verified, probe-passed language model is installed. Never a download."""


@dataclass(frozen=True, slots=True)
class LlmModelInfo:
    """Provenance recorded against every minute this model produces."""

    model_name: str
    revision: str
    manifest_sha256: str
    quantisation: str
    license_name: str
    context_tokens: int
    threads: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "revision": self.revision,
            "manifest_sha256": self.manifest_sha256,
            "quantisation": self.quantisation,
            "license_name": self.license_name,
            "context_tokens": self.context_tokens,
            "threads": self.threads,
        }


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """One prompt. Temperature defaults to zero: a minute is not a creative writing task."""

    system: str
    user: str
    grammar: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.0
    seed: int = 20260808
    stop: tuple[str, ...] = ()


@dataclass(slots=True)
class GenerationResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    finish_reason: str = ""
    truncated: bool = False

    @property
    def tokens_per_second(self) -> float:
        return self.completion_tokens / self.seconds if self.seconds > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Counts and timings. **Never the text** -- that is the caller's to handle."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(self.seconds, 3),
            "tokens_per_second": round(self.tokens_per_second, 2),
            "finish_reason": self.finish_reason,
            "truncated": self.truncated,
        }


class ResolvedLlm:
    """A GGUF file that has been found *and* verified. No constructor takes a raw path."""

    __slots__ = ("path", "manifest", "digest")

    def __init__(self, path: Path, manifest: Any, digest: str) -> None:
        self.path = path
        self.manifest = manifest
        self.digest = digest

    def describe(self) -> dict[str, Any]:
        """Provenance without the path: safe for an API response or a log line."""
        return {
            "model_name": self.manifest.model_name,
            "revision": self.manifest.revision,
            "manifest_sha256": self.digest,
            "license": self.manifest.license_name,
            "source_repo": self.manifest.source_repo,
            "total_bytes": self.manifest.total_bytes,
        }


def resolve_verified_llm_directory(directory: Path, *, deep: bool = False) -> ResolvedLlm:
    """Verify one promoted directory and wrap it. **Provisioning probe only.**

    Bypasses the readiness registry deliberately, for the same reason the ASR equivalent
    does: this is the function that *establishes* a readiness record, so requiring one
    would be circular. The manifest is still verified before anything is loaded, and the
    only caller derives the directory itself rather than accepting it from a request.
    """
    from mom_igd.asr.manifest import ManifestError, manifest_digest, verify_directory

    try:
        manifest = verify_directory(directory, deep=deep)
    except ManifestError as exc:
        raise LlmUnavailableError(f"MODEL_UNAVAILABLE: {exc}") from None
    return ResolvedLlm(
        directory / manifest.files[0].name, manifest, manifest_digest(manifest)
    )


def resolve_llm(models_dir: Path, *, deep: bool = False) -> ResolvedLlm:
    """The language model this build may load right now, or `MODEL_UNAVAILABLE`.

    Reads the **installed registry**, never a directory scan. A GGUF that hashes correctly
    but failed its load probe has no registry entry and must not resolve -- that is the
    defect ADR-0015 was written about, and it applies to a language model exactly as it
    does to an ASR model.
    """
    from mom_igd.asr.installed import load_index
    from mom_igd.asr.manifest import ManifestError, manifest_digest, verify_directory
    from mom_igd.asr.provision import MODEL_CATALOGUE

    index = load_index(models_dir)
    if not index.readable:
        raise LlmUnavailableError(
            f"MODEL_UNAVAILABLE: the installed-model registry could not be read "
            f"({index.problem}). Nothing is treated as ready. Re-run "
            "`asr provision mom-llm`."
        )
    approved = {
        spec.model_name for spec in MODEL_CATALOGUE.values() if spec.kind == "llm"
    }
    ready = [
        entry
        for entry in index.ready(models_dir, role="mom")
        if entry.model_name in approved
    ]
    if not ready:
        raise LlmUnavailableError(
            "MODEL_UNAVAILABLE: no language model is provisioned and probe-passed. "
            "Provision one once, with network access: "
            "`python -m mom_igd asr provision mom-llm`. Minutes extraction never "
            "downloads a model, and never falls back to a different one."
        )
    ready.sort(key=lambda entry: entry.probed_at, reverse=True)
    chosen = ready[0]
    directory = models_dir / chosen.relative_path
    try:
        manifest = verify_directory(
            directory, deep=deep, expected_digest=chosen.manifest_sha256
        )
    except ManifestError as exc:
        raise LlmUnavailableError(f"MODEL_UNAVAILABLE: {exc}") from None
    return ResolvedLlm(
        directory / manifest.files[0].name, manifest, manifest_digest(manifest)
    )


class LocalLlm:
    """A loaded llama.cpp model. Construct in a worker, use, close.

    Never constructed on the API thread, and never beside another heavy model: the
    one-heavy-worker policy (ADR-0004) means these weights live in a process that exits.

    **Measured resident memory at the default settings**, from llama.cpp's own allocation
    report on the target device::

        mapped model weights   2363 MiB   the GGUF, memory-mapped
        CPU_REPACK copy        1683 MiB   llama.cpp re-lays the q4_K weights out for
                                          AVX-512. Not optional in this wheel: no
                                          GGML_NO_REPACK, LLAMA_NO_REPACK or
                                          GGML_CPU_REPACK setting suppresses it, and all
                                          three were tried and measured.
        KV cache               1152 MiB   at n_ctx 8192 (864 MiB at 6144)
        compute buffer          153 MiB   at n_batch 256 (563 MiB at 512)
        ------------------------------
        total                  5351 MiB

    That **exceeds the 2.5 GB heavy-worker budget** ADR-0016 records, and no setting here
    can bring it under: the weights and their repacked copy are 4.0 GB before a single
    token of context. That budget was measured against ASR models, and a four-billion
    parameter model does not fit in it. What is claimed instead is the figure above, on a
    16 GB machine, in a process that exits when the run ends -- see ADR-0018.
    """

    def __init__(
        self,
        resolved: ResolvedLlm,
        *,
        context_tokens: int = DEFAULT_CONTEXT_TOKENS,
        threads: int = DEFAULT_THREADS,
        batch_tokens: int = DEFAULT_BATCH_TOKENS,
    ) -> None:
        self._resolved = resolved
        self._context_tokens = max(512, int(context_tokens))
        self._threads = max(1, int(threads))
        self._batch_tokens = max(32, int(batch_tokens))
        self._model: Any = None
        self._load_seconds = 0.0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def load_seconds(self) -> float:
        return self._load_seconds

    def info(self) -> LlmModelInfo:
        name = self._resolved.path.name
        return LlmModelInfo(
            model_name=self._resolved.manifest.model_name,
            revision=self._resolved.manifest.revision,
            manifest_sha256=self._resolved.digest,
            quantisation=_quantisation_of(name),
            license_name=self._resolved.manifest.license_name,
            context_tokens=self._context_tokens,
            threads=self._threads,
        )

    def load(self) -> LlmModelInfo:
        """Construct the engine from the verified local file."""
        if self._model is not None:
            return self.info()

        assert_llm_offline_environment()
        started = time.perf_counter()
        try:
            from llama_cpp import Llama
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise LlmError(
                "llama-cpp-python is not installed, so minutes extraction cannot run. "
                "It is a pinned runtime dependency; reinstall from requirements.txt."
            ) from None
        try:
            self._model = Llama(
                model_path=str(self._resolved.path),
                n_ctx=self._context_tokens,
                n_threads=self._threads,
                n_batch=self._batch_tokens,
                n_ubatch=self._batch_tokens,
                # No GPU layers. The target machine has an Intel Iris Xe and no CUDA;
                # asking for offload here would either fail or silently do nothing.
                n_gpu_layers=0,
                use_mmap=True,
                # Locking 2.3 GB into RAM on a 16 GB machine that is also running the
                # desktop is the wrong trade -- mmap lets the OS evict what it must.
                use_mlock=False,
                logits_all=False,
                verbose=False,
                seed=20260808,
            )
        except Exception as exc:  # noqa: BLE001 - one type out, no path in the message
            raise LlmError(
                f"the language model could not be loaded ({type(exc).__name__}). The "
                "file passed byte verification, so this is a runtime or quantisation "
                "problem rather than a corrupt download."
            ) from None
        self._load_seconds = time.perf_counter() - started
        _LOG.info(
            "mom.llm.loaded",
            extra={
                "model": self._resolved.manifest.model_name,
                "load_s": round(self._load_seconds, 2),
                "ctx": self._context_tokens,
                "threads": self._threads,
            },
        )
        return self.info()

    def validate_grammar(self, grammar: str) -> None:
        """Parse a GBNF grammar and raise if it is invalid. **Not optional.**

        ``LlamaGrammar.from_string`` looks like a parser and is not one -- it stores the
        string and returns. An invalid grammar therefore travels all the way to the
        sampler, where llama.cpp prints ``parse: error parsing grammar`` to stderr and
        returns a null pointer, and the first thing Python sees is an ``OSError`` with no
        indication that a grammar was involved at all. That is how a rule wrapped across
        two lines for readability got as far as a benchmark run.

        So the grammar is built here, against the real vocabulary, and the sampler is
        freed immediately. The cost is microseconds and it turns a mystery into a message.
        """
        from llama_cpp import llama_cpp as _c

        if self._model is None:
            self.load()
        assert self._model is not None  # noqa: S101 - load() raises otherwise

        sampler = _c.llama_sampler_init_grammar(
            self._model._model.vocab, grammar.encode("utf-8"), b"root"
        )
        if not sampler:
            raise LlmError(
                "the output grammar is not valid GBNF, so llama.cpp refused to build a "
                "sampler for it (the parser's reason is on stderr). Refusing to generate "
                "unconstrained: an unconstrained answer would have to be repaired, and a "
                "repair step is where an invented field gets in. Note that GBNF ends a "
                "rule at the newline -- each rule must be on one line."
            )
        _c.llama_sampler_free(sampler)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """One completion, constrained by a grammar when one is supplied.

        Temperature is zero by default and the seed is fixed, so the same transcript
        produces the same minute. A minute that changed between runs could not be
        reviewed, and Phase 7's reconciliation needs determinism to diff against.
        """
        if self._model is None:
            self.load()
        assert self._model is not None  # noqa: S101 - load() raises otherwise

        grammar = None
        if request.grammar:
            from llama_cpp import LlamaGrammar

            self.validate_grammar(request.grammar)
            grammar = LlamaGrammar.from_string(request.grammar, verbose=False)

        started = time.perf_counter()
        try:
            response = self._model.create_chat_completion(
                messages=[
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                max_tokens=int(request.max_tokens),
                temperature=float(request.temperature),
                seed=int(request.seed),
                grammar=grammar,
                stop=list(request.stop) or None,
            )
        except Exception as exc:  # noqa: BLE001 - never leak the prompt
            raise LlmError(
                f"generation failed ({type(exc).__name__}). No prompt or transcript "
                "text is included in this message by policy."
            ) from None
        elapsed = time.perf_counter() - started

        choice = (response.get("choices") or [{}])[0]
        usage = response.get("usage") or {}
        finish = str(choice.get("finish_reason") or "")
        result = GenerationResult(
            text=str((choice.get("message") or {}).get("content") or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            seconds=elapsed,
            finish_reason=finish,
            # A truncated answer is reported, never silently accepted: half a JSON
            # document that the grammar happened to leave parseable is still half an
            # answer, and the caller has to decide what that means.
            truncated=finish == "length",
        )
        _LOG.info("mom.llm.generated", extra=result.to_dict())
        return result

    def close(self) -> None:
        """Release the weights. Idempotent, and never raises."""
        model, self._model = self._model, None
        if model is not None:
            try:
                model.close()
            except Exception:  # noqa: BLE001 - teardown must not mask a result
                pass
        self._load_seconds = 0.0

    def health(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "context_tokens": self._context_tokens,
            "threads": self._threads,
            "model": self._resolved.describe(),
        }


#: Environment flags that keep llama.cpp and anything it pulls in from reaching out.
#: Assignment, never ``setdefault`` -- an operator shell carrying an override must not be
#: able to put a worker online, exactly as in the ASR provider.
_LLM_OFFLINE_FLAGS: Final[dict[str, str]] = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "LLAMA_CPP_DISABLE_TELEMETRY": "1",
}


def assert_llm_offline_environment() -> dict[str, str]:
    """Put this process into offline mode and return the flags that were set."""
    for key, value in _LLM_OFFLINE_FLAGS.items():
        os.environ[key] = value
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"):
        os.environ.pop(name, None)
    return dict(_LLM_OFFLINE_FLAGS)


def _quantisation_of(filename: str) -> str:
    """`Qwen3-4B-Q4_K_M.gguf` -> `Q4_K_M`. Recorded against every minute produced."""
    stem = filename.rsplit(".", 1)[0]
    for part in reversed(stem.split("-")):
        if part.upper().startswith("Q") and any(ch.isdigit() for ch in part):
            return part.upper()
    return "unknown"
