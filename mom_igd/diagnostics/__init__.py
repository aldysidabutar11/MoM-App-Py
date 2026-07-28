"""Environment diagnostics ("doctor").

The doctor answers one question: *can this machine run the phase of the
application that is currently implemented?* It therefore separates hard
requirements from things that only matter in a later phase.

The result types and the text renderer live in :mod:`mom_igd.diagnostics.model`,
which imports nothing outside the standard library, so
:mod:`mom_igd.diagnostics.bootstrap` can produce a reduced report on an
interpreter where the Phase 1 runtime dependencies are not installed yet. That is
what lets ``py -3.12 -m mom_igd doctor`` answer usefully instead of raising
``ModuleNotFoundError``.

:mod:`mom_igd.diagnostics.doctor` needs pydantic, so it is imported lazily by
:func:`run_doctor` rather than at package import time.
"""

from mom_igd.diagnostics.model import CheckResult, DoctorReport, Status, format_report

__all__ = [
    "CheckResult",
    "DoctorReport",
    "Status",
    "format_report",
    "run_bootstrap_doctor",
    "run_doctor",
]


def run_doctor(*args, **kwargs):
    """Full diagnostics. Requires the Phase 1 runtime dependencies."""
    from mom_igd.diagnostics.doctor import run_doctor as _run

    return _run(*args, **kwargs)


def run_bootstrap_doctor(*args, **kwargs):
    """Reduced, standard-library-only diagnostics (no third-party import)."""
    from mom_igd.diagnostics.bootstrap import run_bootstrap_doctor as _run

    return _run(*args, **kwargs)
