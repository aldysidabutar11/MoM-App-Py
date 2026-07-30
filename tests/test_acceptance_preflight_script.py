"""The operator preflight script: read-only, and it refuses the production root.

Asserted statically. The script is the first thing an operator runs and it runs with
`-ExecutionPolicy Bypass`, so a destructive command hidden in it would execute without a
prompt. These tests are cheap insurance against that, and against the one mistake that
would be expensive: pointing the acceptance run at the production data root, whose
database holds real meetings.

Nothing here executes the script. Running it takes ~20 seconds, loads a model and depends
on this machine's disk and devices; the assertions that matter are about what it *cannot*
do, and those are properties of the text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "phase4_acceptance_preflight.ps1"
PRODUCTION_ROOT = r"D:\MoM-IGD-Data"
ACCEPTANCE_ROOT = r"D:\MoM-IGD-Models-Phase4"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _code(text: str) -> str:
    """The script with comment lines and the help block removed.

    A banned verb named in a comment that *promises the script does not do it* would
    otherwise fail these tests -- which is how a good check gets deleted for crying wolf.
    """
    body = text.split("#>", 1)[1] if "#>" in text else text
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split(" #")[0] if " #" in line else line)
    return "\n".join(lines)


# ===========================================================================
# It exists and is runnable the documented way
# ===========================================================================


def test_the_script_exists_at_the_documented_path() -> None:
    assert SCRIPT.is_file()


def test_the_manual_guide_names_the_same_path() -> None:
    guide = (REPO / "docs" / "phase-4-manual-acceptance.md").read_text(encoding="utf-8")
    assert "scripts\\phase4_acceptance_preflight.ps1" in guide


def test_the_script_defaults_to_the_acceptance_root(script: str) -> None:
    match = re.search(r"\$DataDir\s*=\s*'([^']+)'", script)
    assert match is not None
    assert match.group(1) == ACCEPTANCE_ROOT


def test_the_script_accepts_an_explicit_data_dir(script: str) -> None:
    assert re.search(r"param\(", script)
    assert "[string]$DataDir" in script


def test_the_script_needs_no_administrator_rights(script: str) -> None:
    code = _code(script)
    for forbidden in ("#Requires -RunAsAdministrator", "RunAs", "Start-Process -Verb"):
        assert forbidden not in code, forbidden


# ===========================================================================
# It refuses the production root
# ===========================================================================


def test_the_production_root_is_refused_before_anything_else_runs(script: str) -> None:
    """The guard must precede the first command, not sit somewhere after it."""
    code = _code(script)
    guard = code.index("$normalisedTarget")
    for later in ("Invoke-Mom @(", "Get-CimInstance", "Get-PSDrive"):
        assert code.index(later) > guard, f"{later} runs before the production guard"


def test_the_refusal_compares_case_insensitively_and_ignores_a_trailing_slash(
    script: str,
) -> None:
    """`d:\\mom-igd-data\\` is the same directory, and Windows paths are not case-sensitive."""
    code = _code(script)
    assert "-ieq" in code, "the comparison must be case-insensitive"
    assert code.count("TrimEnd('\\', '/')") >= 2, "a trailing separator must be ignored"


def test_the_refusal_exits_non_zero(script: str) -> None:
    """Sliced from the raw text: the section markers are comments."""
    block = script[script.index("$normalisedTarget") : script.index("# 1. Environment")]
    assert "exit 2" in block


def test_the_production_root_appears_only_as_something_refused(script: str) -> None:
    """It must never be a target, a default or a copy destination."""
    code = _code(script)
    for line in code.splitlines():
        if PRODUCTION_ROOT.lower() not in line.lower():
            continue
        assert "$ProductionRoot" in line or "REFUSED" in line, line


# ===========================================================================
# It changes nothing
# ===========================================================================


@pytest.mark.parametrize(
    "verb",
    [
        "rmdir",
        "del",
        "erase",
        "attrib",
        "icacls",
        "takeown",
        "netsh",
        "bcdedit",
        "diskpart",
    ],
)
def test_no_legacy_shell_command_is_invoked(script: str, verb: str) -> None:
    """Matched as a command at the start of a statement.

    A bare substring search flagged `del ` inside the word "model", which is exactly how
    a check earns a reputation for crying wolf and then gets deleted.
    """
    code = _code(script)
    pattern = rf"(?:^|[;|&(]\s*){re.escape(verb)}"
    assert not re.search(pattern, code, re.IGNORECASE | re.MULTILINE), verb


@pytest.mark.parametrize(
    "verb",
    [
        "Remove-Item",
        "Clear-Content",
        "Set-Content",
        "Add-Content",
        "Out-File",
        "New-Item",
        "Move-Item",
        "Copy-Item",
        "Rename-Item",
        "Format-Volume",
        "Remove-ItemProperty",
        "Set-ItemProperty",
        "New-ItemProperty",
        "reg add",
        "reg delete",
    ],
)
def test_the_script_contains_no_destructive_command(script: str, verb: str) -> None:
    assert verb.lower() not in _code(script).lower(), verb


@pytest.mark.parametrize(
    "verb",
    [
        "Stop-Process",
        "Stop-Service",
        "Stop-Computer",
        "Restart-Computer",
        "Restart-Service",
        "taskkill",
        "wsl --shutdown",
        "wsl --terminate",
        "docker stop",
        "docker kill",
        "Set-Service",
    ],
)
def test_the_script_never_stops_a_user_process_or_service(script: str, verb: str) -> None:
    """Docker and WSL are informational only. This application never stops them."""
    assert verb.lower() not in _code(script).lower(), verb


def test_the_script_never_provisions_or_downloads(script: str) -> None:
    """`provision` may be *named* in a hint, and must never be *invoked*.

    Telling the operator the command to run is the whole point of the message; running it
    would make a read-only preflight download two gigabytes.
    """
    code = _code(script)
    assert "'provision'" not in code, "provisioning must not be invoked"
    assert "asr', 'provision" not in code
    for forbidden in (
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "Start-BitsTransfer",
        "System.Net.WebClient",
        "curl.exe",
        "wget",
    ):
        assert forbidden.lower() not in code.lower(), forbidden
    # Every mention of provisioning is either a message handed to the operator or a test
    # of what the CLI *reported*. Neither can start a download.
    for line in code.splitlines():
        if "provision" not in line.lower():
            continue
        assert (
            "Add-Result" in line or "Write-Host" in line or "-match" in line
        ), line


def test_the_script_never_migrates_a_database(script: str) -> None:
    """`db init` applies migrations. A preflight must not change a schema."""
    code = _code(script)
    assert "'db', 'init'" not in code
    assert "db init" not in code.replace("python -m mom_igd db init --data-dir", "HINT")


def test_the_script_never_opens_the_microphone(script: str) -> None:
    """`audio devices` enumerates without opening a stream; calibrate and probe do not."""
    code = _code(script)
    for forbidden in ("'audio', 'calibrate'", "'audio', 'probe'", "open-test"):
        assert forbidden not in code, forbidden
    assert "'audio', 'devices'" in code


def test_the_script_never_runs_the_benchmark(script: str) -> None:
    """The benchmark takes minutes and is not a readiness check."""
    assert "'asr', 'bench'" not in _code(script)


# ===========================================================================
# What it must actually check
# ===========================================================================


@pytest.mark.parametrize(
    "command",
    [
        "'db', 'version'",
        "'db', 'verify'",
        "'doctor'",
        "'asr', 'models'",
        "'asr', 'verify'",
        "'asr', 'smoke'",
        "'audio', 'devices'",
    ],
)
def test_the_script_runs_every_required_check(script: str, command: str) -> None:
    assert command in _code(script), command


def test_every_check_passes_the_chosen_data_dir(script: str) -> None:
    """A check that silently used the default root would report on the wrong machine."""
    code = _code(script)
    assert "'--data-dir', $DataDir" in code
    # Every invocation goes through the one helper, so there is one place it can be wrong.
    assert code.count("& $Python @all") == 1
    assert len(re.findall(r"&\s*\$Python\s", code)) == 2, (
        "only the helper and the version probe may call the interpreter directly"
    )


def test_the_script_checks_disk_and_memory(script: str) -> None:
    code = _code(script)
    assert "Get-PSDrive" in code
    assert "FreePhysicalMemory" in code


def test_the_script_reports_a_pass_warn_fail_summary(script: str) -> None:
    code = _code(script)
    assert "Summary:" in code
    for status in ("PASS", "WARN", "FAIL"):
        assert f"'{status}'" in code


def test_an_engineering_failure_exits_non_zero(script: str) -> None:
    tail = script[script.index("# Verdict") :]
    assert "$script:FailCount -gt 0" in tail
    assert "exit 1" in tail
    assert "exit 0" in tail


def test_a_warning_does_not_block_the_functional_test(script: str) -> None:
    """The distinction the brief asks for: an acceptance WARN is not an engineering FAIL."""
    tail = script[script.index("# Verdict") :]
    assert "expected at this phase and does not block" in tail


def test_the_script_prints_the_exact_command_to_open_the_gui(script: str) -> None:
    tail = script[script.index("# Verdict") :]
    assert "-m mom_igd shell" in tail
    assert "--data-dir" in tail


def test_the_script_states_that_accuracy_is_not_measured(script: str) -> None:
    """A green preflight must not read as "transcription is correct"."""
    assert "Accuracy has NOT been measured" in script


# ===========================================================================
# It leaks nothing
# ===========================================================================


def test_the_script_prints_no_token_or_credential(script: str) -> None:
    code = _code(script).lower()
    for forbidden in ("token", "password", "secret", "credential", "api_key"):
        assert forbidden not in code, forbidden


def test_the_script_prints_no_private_path_other_than_the_chosen_root(
    script: str,
) -> None:
    """The repository root and the data root are derived, never hard-coded to a user."""
    code = _code(script)
    for forbidden in ("C:\\Users\\", "\\AppData\\", "Aldy", "pangsor"):
        assert forbidden not in code, forbidden


def test_the_script_prints_no_transcript_text(script: str) -> None:
    """It never reads a transcript, so there is nothing to leak."""
    code = _code(script)
    for forbidden in ("'asr', 'transcript'", "transcript_segments", "SELECT "):
        assert forbidden not in code, forbidden
