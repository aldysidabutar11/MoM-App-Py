"""The offline guarantee, and the worker isolation that carries it.

Four claims are proved here, each against the real code rather than by assertion:

1. **A hostile environment cannot put the runtime online.** An operator shell carrying
   `HF_HUB_OFFLINE=0` and an `HF_TOKEN` must not survive into a worker. That means
   assignment, never `setdefault` -- and the difference is exactly what a mutation test
   catches.
2. **The engine is addressed locally.** `local_files_only=True` and an absolute
   directory, so there is nothing for the library to resolve remotely.
3. **The ONNX session runs on CPU.** This build advertises an `AzureExecutionProvider`.
   Its *presence* is not evidence of a network call, and this file does not claim it is;
   what is asserted is that the live VAD session reports `CPUExecutionProvider` and that
   anything else is refused.
4. **A spawned worker cannot reach the network.** Socket, DNS, HTTP and an implicit model
   lookup are all attempted from inside a child process, and all must fail.

Claim 4 needs a real subprocess, so those tests spawn one. They are slow by the standards
of a unit test and cheap by the standards of the guarantee they establish.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ASR = REPO / "mom_igd" / "asr"


def _run_child(body: str, *, env: dict[str, str] | None = None, timeout: int = 600):
    """Run a snippet in a fresh interpreter with the repository importable.

    A fresh process is the point: the offline flags are set at import/entry time, and a
    test that ran in-process would inherit whatever an earlier test already set.
    """
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO)!r})
        {textwrap.indent(textwrap.dedent(body), '        ').lstrip()}
        """
    )
    child_env = dict(os.environ)
    child_env.pop("HF_HUB_OFFLINE", None)
    child_env.pop("TRANSFORMERS_OFFLINE", None)
    if env:
        child_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO),
        env=child_env,
    )


# ===========================================================================
# 1. A hostile environment cannot put the runtime online
# ===========================================================================


HOSTILE_ENV = {
    "HF_HUB_OFFLINE": "0",
    "TRANSFORMERS_OFFLINE": "0",
    "HF_DATASETS_OFFLINE": "0",
    "HF_HUB_DISABLE_TELEMETRY": "0",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "0",
    "HF_HUB_ENABLE_HF_TRANSFER": "1",
    "HF_TOKEN": "hostile-token",
    "HUGGING_FACE_HUB_TOKEN": "hostile-token-2",
    "HUGGINGFACEHUB_API_TOKEN": "hostile-token-3",
}


def test_a_hostile_environment_is_overridden_not_honoured() -> None:
    """`HF_HUB_OFFLINE=0` in the operator's shell must not reach the engine."""
    result = _run_child(
        """
        import json
        from mom_igd.asr.faster_whisper_provider import (
            assert_offline_environment, offline_environment_evidence)
        assert_offline_environment()
        print(json.dumps(offline_environment_evidence()))
        """,
        env=HOSTILE_ENV,
    )
    assert result.returncode == 0, result.stderr
    import json

    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["flags_enforced"] is True, evidence
    assert evidence["flags"]["HF_HUB_OFFLINE"] == "1"
    assert evidence["flags"]["TRANSFORMERS_OFFLINE"] == "1"
    assert evidence["flags"]["HF_HUB_ENABLE_HF_TRANSFER"] == "0"


def test_inherited_credentials_are_scrubbed() -> None:
    """The runtime never authenticates, so carrying a token is gratuitous exposure."""
    result = _run_child(
        """
        import json
        from mom_igd.asr.faster_whisper_provider import (
            assert_offline_environment, offline_environment_evidence)
        assert_offline_environment()
        print(json.dumps(offline_environment_evidence()))
        """,
        env=HOSTILE_ENV,
    )
    assert result.returncode == 0, result.stderr
    import json

    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["credentials_present"] == [], evidence


def test_the_offline_flags_use_assignment_not_setdefault() -> None:
    """Read from the source, because this is the line a "helpful" refactor changes.

    `setdefault` would honour whatever the environment already said, which is the exact
    opposite of a guarantee.
    """
    source = (ASR / "faster_whisper_provider.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "assert_offline_environment"
    )

    # Inspect the AST, not the source text. The docstring *explains* why `setdefault` is
    # wrong, so a substring search flags the sentence forbidding the thing as an
    # instance of the thing -- and that false positive is how a real check gets deleted.
    calls = [
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "setdefault" not in calls, (
        "the offline flags must be assigned; setdefault lets a hostile environment win"
    )

    # And the assignment really is a subscript store into os.environ.
    subscript_stores = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Subscript) for target in node.targets)
    ]
    assert subscript_stores, "no `os.environ[key] = value` assignment found"


def test_no_asr_module_uses_setdefault_for_a_guarantee() -> None:
    """A guarantee expressed with `setdefault` is a preference, not a guarantee."""
    offenders: list[str] = []
    for path in sorted(ASR.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "environ.setdefault" in line:
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == [], offenders


# ===========================================================================
# 2. The engine is addressed locally
# ===========================================================================


def test_the_engine_is_constructed_with_local_files_only() -> None:
    source = (ASR / "faster_whisper_provider.py").read_text(encoding="utf-8")
    assert "local_files_only=True" in source
    assert "local_files_only=False" not in source


def test_the_engine_is_given_a_directory_not_a_hub_id() -> None:
    """A hub id would be resolved remotely; an absolute local path cannot be."""
    source = (ASR / "faster_whisper_provider.py").read_text(encoding="utf-8")
    assert "WhisperModel(\n                str(self._resolved.directory)," in source


def test_no_runtime_module_can_reach_a_download_api() -> None:
    """Only the explicit provisioning command may import a network client."""
    offenders: list[str] = []
    for path in sorted((REPO / "mom_igd").rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        if relative == "mom_igd/asr/provision.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"huggingface_hub", "requests", "aiohttp"}:
                    offenders.append(f"{relative}:{node.lineno} imports {name}")
    assert offenders == [], offenders


# ===========================================================================
# 3. The ONNX session runs on CPU
# ===========================================================================


def test_the_vad_session_reports_the_cpu_execution_provider() -> None:
    """Measured from the live session, not inferred from the capability list."""
    from mom_igd.asr.vad import onnx_provider_evidence

    evidence = onnx_provider_evidence()
    assert evidence.get("session"), f"could not read the session providers: {evidence}"
    assert evidence["session"] == ["CPUExecutionProvider"], evidence
    assert evidence["ok"] is True


def test_a_forbidden_execution_provider_is_refused() -> None:
    """If a wheel upgrade ever selected one, VAD must stop rather than proceed."""
    import mom_igd.asr.vad as vad_module

    original = vad_module.onnx_provider_evidence
    try:
        vad_module.onnx_provider_evidence = lambda: {  # type: ignore[assignment]
            "available": ["AzureExecutionProvider", "CPUExecutionProvider"],
            "session": ["AzureExecutionProvider"],
            "ok": False,
        }
        with pytest.raises(vad_module.VadError, match="does not permit"):
            vad_module._assert_cpu_execution_provider()
    finally:
        vad_module.onnx_provider_evidence = original  # type: ignore[assignment]


def test_the_azure_provider_being_listed_is_not_treated_as_a_network_call() -> None:
    """The claim made in the docs must match the claim made in code.

    Its presence in `get_available_providers()` is a capability listing. What matters is
    the session, and that is what is checked -- so the source must not assert that the
    mere listing implies egress.
    """
    from mom_igd.asr.vad import onnx_provider_evidence

    evidence = onnx_provider_evidence()
    # It is present on this machine...
    assert "AzureExecutionProvider" in evidence["available"]
    # ...and it is not what runs.
    assert "AzureExecutionProvider" not in evidence["session"]


def test_onnx_telemetry_is_disabled_by_assignment() -> None:
    source = (ASR / "vad.py").read_text(encoding="utf-8")
    assert 'os.environ["ORT_DISABLE_ALL_TELEMETRY"] = "1"' in source


# ===========================================================================
# 4. A spawned worker cannot reach the network
# ===========================================================================


def test_the_egress_blocker_actually_blocks_socket_dns_and_http() -> None:
    """The instrument the offline evidence rests on must itself be sound.

    This machine has working internet, so a test cannot prove the OS blocks egress --
    that would be testing the firewall, not the product. What *is* provable, and what
    every other offline claim depends on, is that `no_network()` genuinely intercepts
    every outbound primitive. If the blocker silently failed, "zero attempts recorded"
    would be meaningless.

    Run in a child process so the monkey-patching cannot leak into the rest of the suite.
    """
    result = _run_child(
        """
        import json, socket, urllib.request
        from mom_igd.asr.smoke import no_network

        outcomes = {}
        blocker = no_network()
        with blocker:
            for label, call in (
                ("tcp", lambda: socket.create_connection(("huggingface.co", 443), timeout=3)),
                ("dns", lambda: socket.getaddrinfo("huggingface.co", 443)),
                ("http", lambda: urllib.request.urlopen("https://huggingface.co", timeout=3)),
            ):
                try:
                    call()
                    outcomes[label] = "REACHED"
                except OSError as exc:
                    outcomes[label] = "blocked" if "blocked by the offline" in str(exc) else f"other:{exc}"
                except Exception as exc:
                    outcomes[label] = f"unexpected:{type(exc).__name__}"
        outcomes["recorded_attempts"] = sorted(set(blocker.attempts))

        # And after the block exits, the real functions are restored.
        try:
            socket.getaddrinfo("huggingface.co", 443)
            outcomes["restored"] = True
        except Exception as exc:
            outcomes["restored"] = f"NOT restored: {type(exc).__name__}"
        print(json.dumps(outcomes))
        """,
        env=HOSTILE_ENV,
    )
    assert result.returncode == 0, result.stderr
    import json

    outcomes = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcomes["tcp"] == "blocked", outcomes
    assert outcomes["dns"] == "blocked", outcomes
    assert outcomes["http"] == "blocked", outcomes
    # Each recorded attempt names the primitive *and* the target, so a future non-empty
    # list says where something tried to go rather than only that it tried.
    primitives = {attempt.split(" -> ")[0] for attempt in outcomes["recorded_attempts"]}
    assert primitives >= {
        "socket.create_connection",
        "socket.getaddrinfo",
        "urllib.urlopen",
    }, outcomes
    assert all("huggingface.co" in attempt for attempt in outcomes["recorded_attempts"]), (
        outcomes
    )
    assert outcomes["restored"] is True, (
        "the blocker must restore the real primitives, or it would poison later tests"
    )


def test_an_implicit_model_lookup_cannot_reach_the_network() -> None:
    """A repository that is definitely not cached must not resolve in offline mode.

    An earlier version of this test asked for `Systran/faster-whisper-small`, which is in
    this machine's shared Hugging Face cache from provisioning -- so
    `snapshot_download` "succeeded" **from cache** and the test failed for the wrong
    reason. That is precisely the trap the brief warns about: offline must not mean
    "a cache might happen to have it".

    So the request is for an artefact that cannot be cached. Success would mean the
    offline flags were not honoured and the process reached the network.
    """
    result = _run_child(
        """
        from mom_igd.asr.faster_whisper_provider import assert_offline_environment
        assert_offline_environment()
        try:
            from huggingface_hub import snapshot_download
            snapshot_download("mom-igd-offline-probe/definitely-not-cached-000")
            print("RESULT=REACHED_NETWORK")
        except Exception as exc:
            print(f"RESULT=REFUSED:{type(exc).__name__}")
        """,
        env=HOSTILE_ENV,
    )
    assert result.returncode == 0, result.stderr
    line = [ln for ln in result.stdout.splitlines() if ln.startswith("RESULT=")][-1]
    assert line.startswith("RESULT=REFUSED"), (
        f"a hub lookup reached the network despite offline mode: {line}"
    )


def test_the_product_never_resolves_a_model_through_the_hub_cache() -> None:
    """The property that actually matters, asserted against the product.

    The shared Hugging Face cache does contain a copy of the pass-1 model, left there by
    provisioning. The runtime must not care: it addresses an absolute directory inside the
    model store, verified against a manifest and a readiness record. Point it at an empty
    store and it must fail closed even though the cache is populated.
    """
    import tempfile

    from mom_igd.asr.faster_whisper_provider import resolve_model
    from mom_igd.asr.provider import ModelUnavailableError

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        empty_store = Path(scratch) / "models"
        empty_store.mkdir()
        with pytest.raises(ModelUnavailableError, match="MODEL_UNAVAILABLE"):
            resolve_model(empty_store, role="pass1")


def test_the_runtime_does_not_consult_a_cache_directory_at_all() -> None:
    """No runtime module names a hub cache environment variable or path."""
    offenders: list[str] = []
    for path in sorted(ASR.glob("*.py")):
        if path.name == "provision.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if value in {"HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"} or (
                    "huggingface/hub" in value
                ):
                    offenders.append(f"{path.name}:{node.lineno} {value!r}")
    assert offenders == [], offenders


def test_a_worker_loads_a_model_with_every_outbound_primitive_blocked(
    tmp_path: Path,
) -> None:
    """The strongest available offline evidence short of pulling the cable.

    Skipped when no model is provisioned, because that is a machine-provisioning state
    rather than a code defect -- and the skip reason says so rather than passing quietly.
    """
    store = os.environ.get("MOM_IGD_ASR_MODEL_STORE")
    candidates = [Path(store)] if store else [
        Path(r"D:\MoM-IGD-Models-Phase4\models"),
    ]
    models_dir = next((c for c in candidates if c.is_dir()), None)
    if models_dir is None:
        pytest.skip(
            "no provisioned model store found; run `asr provision all` and set "
            "MOM_IGD_ASR_MODEL_STORE to exercise the real-model offline path"
        )

    result = _run_child(
        f"""
        import json
        from pathlib import Path
        from mom_igd.asr.faster_whisper_provider import (
            FasterWhisperProvider, assert_offline_environment, resolve_model)
        from mom_igd.asr.provider import ModelUnavailableError, TranscriptionRequest
        from mom_igd.asr.smoke import generate_speech_like_wav, no_network

        assert_offline_environment()
        try:
            resolved = resolve_model(Path({str(models_dir)!r}), role="pass1")
        except ModelUnavailableError as exc:
            print(json.dumps({{"ok": False, "reason": "MODEL_UNAVAILABLE"}}))
            raise SystemExit(0)

        audio = Path({str(tmp_path)!r}) / "probe.wav"
        generate_speech_like_wav(audio, 2.0)

        blocker = no_network()
        with blocker:
            provider = FasterWhisperProvider(resolved, cpu_threads=2)
            provider.load()
            provider.transcribe(TranscriptionRequest(
                audio_path=str(audio), regions=(), language="id", beam_size=1))
            provider.close()
        print(json.dumps({{"ok": True, "attempts": blocker.attempts}}))
        """,
        env=HOSTILE_ENV,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr
    import json

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    if not payload.get("ok"):
        pytest.skip(f"model not resolvable in the child: {payload.get('reason')}")
    assert payload["attempts"] == [], (
        f"the worker attempted an outbound connection while loading a local model: "
        f"{payload['attempts']}"
    )


def test_the_worker_sets_offline_flags_before_importing_the_engine() -> None:
    """Order matters: the libraries read these at import time."""
    source = (ASR / "worker.py").read_text(encoding="utf-8")
    entry = source[source.index("def _child_entrypoint") :]
    offline_at = entry.index("assert_offline_environment()")
    registry_at = entry.index("from mom_igd.asr.tasks import TASK_REGISTRY")
    assert offline_at < registry_at, (
        "the worker must put itself offline before importing anything that could reach "
        "the network"
    )


def test_the_worker_uses_spawn_not_fork() -> None:
    """Windows has no fork, and inheriting a parent's state is how a lock leaks."""
    source = (ASR / "worker.py").read_text(encoding="utf-8")
    assert 'mp.get_context("spawn")' in source
    assert "fork" not in source.replace("no fork", "").replace(
        "does not depend on ``fork``", ""
    ).replace("nothing depends on ``fork``", "")


def test_the_worker_never_prints_transcript_text() -> None:
    """Worker output lands in logs, and a transcript in a log is a privacy incident."""
    source = (ASR / "worker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    prints = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert prints == [], "the worker must not print; it returns a payload"

    tasks_source = (ASR / "tasks.py").read_text(encoding="utf-8")
    task_prints = [
        node
        for node in ast.walk(ast.parse(tasks_source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert task_prints == []


def test_a_worker_error_message_carries_no_transcript() -> None:
    """The child truncates and reports only the exception type plus a short message."""
    source = (ASR / "worker.py").read_text(encoding="utf-8")
    assert 'f"{type(exc).__name__}: {str(exc)[:300]}"' in source


def test_the_provider_error_path_states_that_it_withholds_text() -> None:
    source = (ASR / "faster_whisper_provider.py").read_text(encoding="utf-8")
    assert "No transcript text" in source


def test_an_unknown_worker_task_is_refused() -> None:
    """The parent sends a task name from a closed set; arbitrary code is not runnable."""
    from mom_igd.asr.worker import run_in_worker

    outcome = run_in_worker("definitely-not-a-task", {}, timeout_seconds=120)
    assert outcome.ok is False
    assert "unknown worker task" in (outcome.error or "")
