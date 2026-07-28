"""Reduced, standard-library-only diagnostics.

**Why this exists.** The most useful moment to run `doctor` is on a machine that is
*not* set up yet — and on such a machine the core runtime dependencies
(pydantic, psutil, ...) are missing, so the full doctor cannot even be imported.
A traceback is a poor answer to "why doesn't this work?".

This module therefore answers the question with no third-party import at all:
which interpreter is this, is it the right version, is it a Store shim, where
would runtime data go, is that location writable, and exactly which dependencies
are missing together with the command that installs them.

It reports a ``FAIL`` for the missing dependencies, so the exit code is ``1`` and
automation still sees a failure — while a human gets a diagnosis instead of a
stack trace.

Invoked automatically by ``mom_igd.cli`` when a required runtime dependency is
absent from the running interpreter. Never a substitute for the full doctor.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from typing import Final

from mom_igd.diagnostics.model import (
    CheckResult,
    DoctorReport,
    Status,
    nearest_existing,
    utc_now_iso,
)
from mom_igd.paths import PathValidationError, RuntimePaths, resolve_data_root
from mom_igd.version import APP_NAME, APP_VERSION, CURRENT_PHASE, version_info

__all__ = ["REQUIRED_RUNTIME_MODULES", "missing_runtime_modules", "run_bootstrap_doctor"]

_REQUIRED_PYTHON: Final[tuple[int, int]] = (3, 12)

_STORE_SHIM_MARKERS: Final[tuple[str, ...]] = (
    "windowsapps",
    "microsoft\\windowsapps",
    "packages\\pythonsoftwarefoundation.python",
)

REQUIRED_RUNTIME_MODULES: Final[tuple[tuple[str, str], ...]] = (
    ("pydantic", "configuration and model-registry validation"),
    ("psutil", "memory and process diagnostics"),
    ("fastapi", "local loopback API"),
    ("uvicorn", "ASGI server for the local API"),
    ("webview", "desktop shell (pywebview)"),
)
"""Imports the *full* doctor needs, with what each is needed for.

``sounddevice`` is deliberately absent even though it is a required runtime
dependency from Phase 2 onwards: it is imported lazily, so the full doctor runs
without it and reports the missing backend as a proper ``FAIL`` with an install
hint. Listing it here would drop the whole run down to this reduced report and
lose every other check.
"""

_INSTALL_HINT: Final[str] = (
    r".venv\Scripts\python.exe -m pip install -r requirements.txt"
)


def _looks_like_store_shim(path: str) -> bool:
    lowered = path.replace("/", "\\").lower()
    return any(marker in lowered for marker in _STORE_SHIM_MARKERS)


def missing_runtime_modules() -> list[tuple[str, str]]:
    """Return the core runtime modules that cannot be imported here."""
    missing: list[tuple[str, str]] = []
    for module, purpose in REQUIRED_RUNTIME_MODULES:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):  # pragma: no cover - odd namespace pkgs
            found = False
        if not found:
            missing.append((module, purpose))
    return missing


def _check_dependencies(missing: list[tuple[str, str]]) -> CheckResult:
    in_venv = sys.prefix != sys.base_prefix
    names = ", ".join(module for module, _ in missing)
    detail = (
        f"{len(missing)} of {len(REQUIRED_RUNTIME_MODULES)} core runtime "
        f"dependencies are not importable by this interpreter ({names}). "
    )
    if in_venv:
        detail += f"Install them with: {_INSTALL_HINT}"
    else:
        detail += (
            "This interpreter is not the project virtual environment. Create and "
            "use it: py -3.12 -m venv .venv  then  " + _INSTALL_HINT
        )
    return CheckResult(
        key="runtime_dependencies",
        title="Core runtime dependencies",
        status=Status.FAIL,
        detail=detail,
        required_in_phase="1",
        data={
            "missing": [{"module": m, "purpose": p} for m, p in missing],
            "interpreter": sys.executable,
            "in_virtualenv": in_venv,
            "install_command": _INSTALL_HINT,
        },
    )


def run_bootstrap_doctor(
    data_root: str | os.PathLike[str] | None = None,
    *,
    missing: list[tuple[str, str]] | None = None,
) -> DoctorReport:
    """Run the standard-library-only subset of the diagnostics.

    Creates nothing and changes nothing, exactly like the full doctor.
    """
    absent = missing_runtime_modules() if missing is None else missing
    results: list[CheckResult] = [
        CheckResult(
            key="application",
            title="Application identity",
            status=Status.PASS,
            detail=f"{APP_NAME} {APP_VERSION} (roadmap phase {CURRENT_PHASE})",
            required_in_phase="1",
            data=version_info(),
        )
    ]

    # -- interpreter --------------------------------------------------------
    actual = sys.version_info[:2]
    interpreter_detail = (
        f"{platform.python_version()} at {sys.executable} "
        f"({platform.architecture()[0]})"
    )
    results.append(
        CheckResult(
            key="python_version",
            title="Python interpreter version",
            status=Status.PASS if actual == _REQUIRED_PYTHON else Status.FAIL,
            detail=(
                interpreter_detail
                if actual == _REQUIRED_PYTHON
                else f"{interpreter_detail} -- this build requires Python "
                f"{_REQUIRED_PYTHON[0]}.{_REQUIRED_PYTHON[1]}.x"
            ),
            required_in_phase="1",
            data={
                "version": platform.python_version(),
                "executable": sys.executable,
                "base_prefix": sys.base_prefix,
                "in_virtualenv": sys.prefix != sys.base_prefix,
            },
        )
    )

    is_shim = _looks_like_store_shim(sys.executable or "") or _looks_like_store_shim(
        sys.base_prefix or ""
    )
    results.append(
        CheckResult(
            key="python_not_store_shim",
            title="Interpreter is not a Microsoft Store shim",
            status=Status.FAIL if is_shim else Status.PASS,
            detail=(
                f"Interpreter resolves through WindowsApps ({sys.base_prefix}); use "
                "the official python.org per-user installation"
                if is_shim
                else f"Official distribution (base prefix {sys.base_prefix})"
            ),
            required_in_phase="1",
            data={"executable": sys.executable, "base_prefix": sys.base_prefix},
        )
    )

    # -- platform -----------------------------------------------------------
    system = platform.system()
    results.append(
        CheckResult(
            key="operating_system",
            title="Operating system",
            status=Status.PASS if system == "Windows" else Status.WARN,
            detail=f"{system} {platform.release()} build {platform.version()}",
            required_in_phase="1" if system == "Windows" else "2",
            data={"system": system, "release": platform.release()},
        )
    )
    results.append(
        CheckResult(
            key="cpu",
            title="CPU",
            status=Status.PASS,
            detail=f"{platform.processor() or 'unknown'}; {os.cpu_count() or 0} logical processor(s)",
            required_in_phase="1",
            data={"processor": platform.processor(), "logical": os.cpu_count()},
        )
    )
    results.append(
        CheckResult(
            key="ram",
            title="System memory",
            status=Status.WARN,
            detail=(
                "Not measured: psutil is unavailable in this interpreter. The full "
                "doctor reports total and available memory."
            ),
            required_in_phase="1",
            data={},
        )
    )

    # -- runtime data root --------------------------------------------------
    try:
        root = resolve_data_root(data_root)
    except PathValidationError as exc:
        results.append(
            CheckResult(
                key="data_path",
                title="Runtime data directory",
                status=Status.FAIL,
                detail=str(exc),
                required_in_phase="1",
            )
        )
        results.append(_check_dependencies(absent) if absent else _all_present())
        return DoctorReport(
            generated_at=utc_now_iso(),
            app=version_info(),
            results=tuple(results),
            mode="bootstrap",
        )

    paths = RuntimePaths(root=root)
    probe = nearest_existing(root)
    try:
        usage = shutil.disk_usage(probe)
        free_gb = usage.free / (1024**3)
        disk = CheckResult(
            key="disk",
            title="Free disk space on the runtime data volume",
            status=Status.PASS,
            detail=f"{free_gb:.1f} GB free at {probe}",
            required_in_phase="1",
            data={"probe_path": str(probe), "free_gb": round(free_gb, 1)},
        )
    except OSError as exc:
        disk = CheckResult(
            key="disk",
            title="Free disk space on the runtime data volume",
            status=Status.FAIL,
            detail=f"Cannot read disk usage for {probe}: {exc}",
            required_in_phase="1",
        )
    results.append(disk)

    results.append(
        CheckResult(
            key="data_path",
            title="Runtime data directory",
            status=Status.PASS,
            detail=(
                f"{root}"
                + ("" if paths.exists() else " (not created yet; run `db init`)")
            ),
            required_in_phase="1",
            data=paths.describe(),
        )
    )
    writable = paths.is_writable()
    results.append(
        CheckResult(
            key="data_path_writable",
            title="Runtime data directory is writable",
            status=Status.PASS if writable else Status.FAIL,
            detail=(
                f"Write probe succeeded at {probe}"
                if writable
                else f"Cannot write under {probe}"
            ),
            required_in_phase="1",
            data={"probe_path": str(probe), "writable": writable},
        )
    )

    # -- the reason we are in bootstrap mode --------------------------------
    results.append(_check_dependencies(absent) if absent else _all_present())

    return DoctorReport(
        generated_at=utc_now_iso(),
        app=version_info(),
        results=tuple(results),
        mode="bootstrap",
    )


def _all_present() -> CheckResult:  # pragma: no cover - bootstrap implies absence
    return CheckResult(
        key="runtime_dependencies",
        title="Core runtime dependencies",
        status=Status.PASS,
        detail="All core runtime dependencies are importable.",
        required_in_phase="1",
        data={"missing": []},
    )
