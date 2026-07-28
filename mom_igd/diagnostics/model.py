"""Diagnostic result types and rendering.

**Standard library only.** This module is imported by
:mod:`mom_igd.diagnostics.bootstrap`, which has to work on an interpreter where
the core runtime dependencies are not installed yet. Adding a third-party
import here would break `py -3.12 -m mom_igd doctor` on a fresh machine, which is
exactly the case the doctor exists to diagnose.

Classification contract:

* ``PASS`` -- required by the current phase, and satisfied.
* ``WARN`` -- optional, informational, or required only in a *future* phase.
* ``FAIL`` -- required by the current phase, and not satisfied.

Exit codes: ``0`` no FAIL · ``1`` any FAIL · ``2`` ``--strict`` with a WARN.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from mom_igd.version import APP_NAME, APP_VERSION, CURRENT_PHASE

__all__ = [
    "CheckResult",
    "DoctorReport",
    "Status",
    "format_report",
    "nearest_existing",
    "utc_now_iso",
]


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def nearest_existing(path: Path) -> Path:
    """Walk up until an existing directory is found (used for disk/write probes)."""
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One diagnostic result."""

    key: str
    title: str
    status: Status
    detail: str
    required_in_phase: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """The full diagnostic run."""

    generated_at: str
    app: dict[str, Any]
    results: tuple[CheckResult, ...]
    mode: str = "full"
    """``full`` for the normal run, ``bootstrap`` when dependencies are missing."""

    @property
    def counts(self) -> dict[str, int]:
        return {
            Status.PASS.value: sum(1 for r in self.results if r.status is Status.PASS),
            Status.WARN.value: sum(1 for r in self.results if r.status is Status.WARN),
            Status.FAIL.value: sum(1 for r in self.results if r.status is Status.FAIL),
        }

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is Status.FAIL)

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is Status.WARN)

    @property
    def ok(self) -> bool:
        """``True`` when nothing required by the current phase is missing."""
        return not self.failures

    def exit_code(self, *, strict: bool = False) -> int:
        if self.failures:
            return 1
        if strict and self.warnings:
            return 2
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "app": self.app,
            "mode": self.mode,
            "counts": self.counts,
            "ok": self.ok,
            "results": [r.to_dict() for r in self.results],
        }


_MODE_BANNER: Final[dict[str, str]] = {
    "full": "environment diagnostics",
    "bootstrap": "environment diagnostics (REDUCED: runtime dependencies missing)",
}


def format_report(report: DoctorReport, *, strict: bool = False) -> str:
    """Render the report as aligned plain text for the console."""
    width = max((len(r.key) for r in report.results), default=10)
    banner = _MODE_BANNER.get(report.mode, _MODE_BANNER["full"])
    lines = [
        f"{APP_NAME} {APP_VERSION} - {banner} (roadmap phase {CURRENT_PHASE})",
        f"Generated {report.generated_at}",
        "",
    ]
    for result in report.results:
        lines.append(f"[{result.status.value:<4}] {result.key:<{width}}  {result.detail}")

    counts = report.counts
    lines += [
        "",
        f"Summary: {counts['PASS']} PASS, {counts['WARN']} WARN, {counts['FAIL']} FAIL",
    ]
    if report.failures:
        lines.append(
            "Result: NOT READY - the checks above marked FAIL are required by the "
            "current phase."
        )
    elif report.warnings:
        lines.append(
            f"Result: READY for phase {CURRENT_PHASE} - warnings are expected "
            "(future-phase dependencies, informational items)."
        )
    else:
        lines.append("Result: READY - no warnings.")
    lines.append(f"Exit code: {report.exit_code(strict=strict)}")
    return "\n".join(lines)
