"""The offline bundle: what it must not be able to do, asserted from the text.

The bundle is the artefact a colleague actually receives. They extract it and
double-click a `.bat` that runs PowerShell with `-ExecutionPolicy Bypass`, so anything
hidden in these scripts executes on their laptop without a prompt. They will not read
them first; nobody does.

Four properties are worth guarding, because each one could stop being true through an
edit that looks harmless:

* the installer reaches the network for nothing. A `pip install` that lost `--no-index`
  still works on the machine that built the bundle -- it silently falls back to PyPI --
  and fails on the laptop in the meeting room with no Wi-Fi, which is the one case the
  bundle exists for;
* the only thing it deletes is the virtual environment it created itself;
* every placeholder in the configuration template is filled by somebody. One that is
  not produces a `config/local.toml` containing `{{RASIO_PASS2}}`, and the application
  refuses to start with a parse error nobody can act on;
* the guide keeps saying the output is a draft, keeps saying speakers are not
  identified, and claims no accuracy figure. Those three are the difference between a
  useful tool and a confident one.

Nothing here builds a bundle: that needs the network, several gigabytes of model
weights and about ten minutes. The properties that matter are properties of the text.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent
PACKAGING = REPO / "packaging"
BUNDLE = PACKAGING / "bundle"
SCRIPTS = BUNDLE / "scripts"
TEMPLATE = BUNDLE / "bahan" / "local.toml.templat"

BAT_FILES = ["1-PASANG.bat", "2-JALANKAN.bat", "3-PERIKSA.bat"]
PS_FILES = ["pasang.ps1", "jalankan.ps1", "periksa.ps1"]

# Every marker in the configuration template, and who is responsible for filling it.
# `{{DATA_ROOT}}` is the odd one out: it cannot be known until somebody chooses a folder
# on the machine being installed, so the installer fills it and the builder must not.
FILLED_BY_BUILDER = {"KAPASITAS", "KAPASITAS_MAKSIMUM", "RASIO_PASS2", "BLOK_BRANDING"}
FILLED_BY_INSTALLER = {"DATA_ROOT"}


def _code(text: str) -> str:
    """PowerShell with comment lines and trailing comments removed.

    A cmdlet named in a comment that promises the script does not call it would
    otherwise fail these tests, which is how a good check gets deleted for crying wolf.
    """
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        lines.append(line.split(" #", 1)[0])
    return "\n".join(lines)


@pytest.fixture(scope="module")
def builder() -> ModuleType:
    """`build_bundle.py`, imported by path.

    By path rather than by adding `packaging/` to `sys.path`: the directory is not a
    package, and a stray import of it from elsewhere in the suite would be a surprise.
    """
    spec = importlib.util.spec_from_file_location(
        "build_bundle", PACKAGING / "build_bundle.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# The pieces exist, and refer to each other correctly
# ===========================================================================


def test_every_documented_bundle_file_exists() -> None:
    for name in BAT_FILES:
        assert (BUNDLE / name).is_file(), f"missing {name}"
    for name in PS_FILES:
        assert (SCRIPTS / name).is_file(), f"missing scripts/{name}"
    assert TEMPLATE.is_file()
    assert (BUNDLE / "PANDUAN.md").is_file()
    assert (PACKAGING / "build_bundle.py").is_file()


@pytest.mark.parametrize("name", BAT_FILES)
def test_each_bat_runs_a_script_that_is_there(name: str) -> None:
    """A `.bat` naming a script that does not exist fails after the double-click."""
    text = (BUNDLE / name).read_text(encoding="utf-8")
    referenced = re.findall(r"%~dp0(scripts[\\/][\w.-]+)", text)
    assert referenced, f"{name} runs no script"
    for relative in referenced:
        assert (BUNDLE / relative.replace("\\", "/")).is_file(), (
            f"{name} runs {relative}, which does not exist"
        )


def test_each_bat_bypasses_execution_policy_only_for_its_own_process() -> None:
    """`-ExecutionPolicy Bypass` on the command line, never `Set-ExecutionPolicy`.

    The flag applies to one process. The cmdlet changes the machine, which this
    application never does.
    """
    for name in BAT_FILES:
        text = (BUNDLE / name).read_text(encoding="utf-8")
        assert "-ExecutionPolicy Bypass" in text
        assert "Set-ExecutionPolicy" not in text


def test_the_builder_requires_the_entries_it_actually_ships(builder: ModuleType) -> None:
    """Anything the builder verifies must exist in `packaging/bundle/` to be shipped.

    Excluding the parts assembled at build time -- source, wheels, models -- which have
    no counterpart in the repository.
    """
    assembled = ("app/", "vendor/", "bahan/models/")
    for entry in builder.REQUIRED_ENTRIES:
        if entry.startswith(assembled):
            continue
        expected = BUNDLE / entry
        assert expected.is_file(), f"the builder requires {entry}, which is not in bundle/"


# ===========================================================================
# The installer never reaches the network
# ===========================================================================


@pytest.mark.parametrize("name", PS_FILES)
def test_no_bundle_script_downloads_anything(name: str) -> None:
    code = _code((SCRIPTS / name).read_text(encoding="utf-8"))
    for cmdlet in (
        "Invoke-WebRequest", "Invoke-RestMethod", "Start-BitsTransfer",
        "System.Net.WebClient", "curl", "wget",
    ):
        assert cmdlet not in code, f"{name} can fetch over the network via {cmdlet}"


def test_every_pip_install_refuses_an_index() -> None:
    """Without `--no-index`, a missing wheel is silently fetched from PyPI.

    That passes on the machine that built the bundle and fails on the one that needed
    it -- and it fails at the point where the operator has already been told there is
    nothing left to arrange.
    """
    code = _code((SCRIPTS / "pasang.ps1").read_text(encoding="utf-8"))
    installs = [line for line in code.splitlines() if "pip install" in line]
    assert installs, "the installer must install something"
    for line in installs:
        assert "--no-index" in line, f"pip install without --no-index: {line.strip()}"
        assert "--find-links" in line, f"pip install without a local wheelhouse: {line.strip()}"


def test_the_installer_also_sets_pip_no_index_in_the_environment() -> None:
    """Belt and braces: the flag covers the command, the variable covers a sub-invocation."""
    code = _code((SCRIPTS / "pasang.ps1").read_text(encoding="utf-8"))
    assert "PIP_NO_INDEX" in code


# ===========================================================================
# The installer changes nothing outside the folder it was extracted into
# ===========================================================================


@pytest.mark.parametrize("name", PS_FILES)
def test_the_only_thing_deleted_recursively_is_the_virtual_environment(name: str) -> None:
    code = _code((SCRIPTS / name).read_text(encoding="utf-8"))
    for line in code.splitlines():
        if "Remove-Item" not in line or "-Recurse" not in line:
            continue
        assert ".venv" in line, f"{name} deletes a directory tree that is not .venv: {line.strip()}"


@pytest.mark.parametrize("name", PS_FILES)
def test_no_bundle_script_changes_system_configuration(name: str) -> None:
    """No registry, no PATH, no firewall, no audio device settings.

    Every one of these is a hard constraint of the product, and the installer is the
    place where "just this once" would be most tempting.
    """
    code = _code((SCRIPTS / name).read_text(encoding="utf-8"))
    for forbidden in (
        "Set-ItemProperty", "New-ItemProperty", "reg add", "reg.exe",
        "HKLM:", "HKCU:", "setx", "netsh", "Set-NetFirewall", "New-NetFirewallRule",
        "[Environment]::SetEnvironmentVariable",
    ):
        assert forbidden not in code, f"{name} changes system configuration via {forbidden}"


def test_the_python_installer_is_offered_per_user_and_never_touches_path() -> None:
    """`PrependPath=0`: the bundle must not rearrange an existing toolchain.

    A colleague may already depend on `python` meaning something else.
    """
    code = _code((SCRIPTS / "pasang.ps1").read_text(encoding="utf-8"))
    assert "InstallAllUsers=0" in code
    assert "PrependPath=0" in code


def test_the_installer_rejects_the_microsoft_store_interpreter() -> None:
    """Its filesystem redirection breaks native module loading, and both engines are native."""
    code = _code((SCRIPTS / "pasang.ps1").read_text(encoding="utf-8"))
    assert "WindowsApps" in code, "the Store shim must be excluded by path"


# ===========================================================================
# The configuration template
# ===========================================================================


def test_every_template_placeholder_is_filled_by_somebody(builder: ModuleType) -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    found = set(re.findall(r"\{\{(\w+)\}\}", template))
    assert found == FILLED_BY_BUILDER | FILLED_BY_INSTALLER, (
        f"the template's placeholders changed: {sorted(found)}"
    )

    source = (PACKAGING / "build_bundle.py").read_text(encoding="utf-8")
    for marker in FILLED_BY_BUILDER:
        assert f'"{{{{{marker}}}}}"' in source, f"the builder never fills {marker}"

    installer = (SCRIPTS / "pasang.ps1").read_text(encoding="utf-8")
    for marker in FILLED_BY_INSTALLER:
        assert f"{{{{{marker}}}}}" in installer, f"the installer never fills {marker}"


def test_the_builder_refuses_to_leave_a_placeholder_unfilled(builder: ModuleType) -> None:
    """The check exists, so a sixth placeholder cannot be added and forgotten."""
    source = (PACKAGING / "build_bundle.py").read_text(encoding="utf-8")
    assert "a placeholder in local.toml.templat was not filled" in source


def test_the_installer_also_refuses_a_template_it_could_not_finish() -> None:
    """Both ends check, because the builder's check can be bypassed.

    A bundle assembled by hand, or a `bahan/` patched after the fact, reaches the
    installer with markers intact. Without this the installer writes the broken file
    anyway and dies two steps later with `Pembuatan basis data gagal` over ten lines of
    tomllib traceback -- none of which tells the operator what to do. Found by doing
    exactly that by accident.
    """
    code = _code((SCRIPTS / "pasang.ps1").read_text(encoding="utf-8"))
    assert "Templat konfigurasi belum lengkap" in code, "the installer must say so plainly"

    pattern = re.search(r"\[regex\]::Matches\(\$isi, '([^']+)'\)", code)
    assert pattern, "the installer must scan for unfilled markers"
    # Applied to the real marker names rather than asserted as a string: the first
    # version of this guard used `[A-Z_]+`, which silently skips `{{RASIO_PASS2}}`
    # because of the digit. A guard with a hole in it is worse than none, since it
    # reads as coverage.
    compiled = re.compile(pattern.group(1))
    for marker in FILLED_BY_BUILDER | FILLED_BY_INSTALLER:
        assert compiled.fullmatch("{{" + marker + "}}"), (
            f"the installer's pattern does not match {{{{{marker}}}}}"
        )


def test_the_data_root_placeholder_carries_no_quotes_of_its_own() -> None:
    """The installer owns the quoting, because only it knows the value.

    TOML gives a backslash two meanings depending on the quote: inside `'literal'` it is
    a plain character, inside `"basic"` it starts an escape. A Windows path is mostly
    backslashes, so a template that picks one quote and an installer that escapes for
    the other disagree silently -- which is exactly what happened. Doubling the
    backslashes inside single quotes stored `D:\\\\MoM-IGD-Data` verbatim; it went
    unnoticed only because `pathlib` collapses repeated separators, and a UNC path
    would not have been so forgiving.
    """
    line = next(
        raw for raw in TEMPLATE.read_text(encoding="utf-8").splitlines()
        if raw.startswith("data_root")
    )
    assert line.strip() == "data_root = {{DATA_ROOT}}", (
        f"the placeholder must be unquoted in the template, found: {line.strip()}"
    )


def test_the_installer_writes_the_data_root_as_an_escaped_basic_string() -> None:
    code = _code((SCRIPTS / "pasang.ps1").read_text(encoding="utf-8"))
    assert ".Replace('\\', '\\\\')" in code, "backslashes must be escaped"
    assert ".Replace('\"', '\\\"')" in code, "quotes must be escaped"
    assert "'\"' + $escaped + '\"'" in code, "the value must be wrapped in double quotes"


@pytest.mark.parametrize(
    "data_root",
    [
        r"C:\MoM-IGD-Data",
        r"D:\Meeting Minutes\data",
        r"\\fileserver\share\MoM-IGD",  # UNC: the case the old rule corrupted
        'C:\\it\'s odd\\data',  # an apostrophe cannot appear in a TOML literal string
    ],
)
def test_a_substituted_template_parses_back_to_the_same_path(data_root: str) -> None:
    """Mirrors what `pasang.ps1` does, then reads the result the way the app does."""
    import tomllib

    escaped = data_root.replace("\\", "\\\\").replace('"', '\\"')
    filled = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("{{DATA_ROOT}}", f'"{escaped}"')
        .replace("{{KAPASITAS}}", "9")
        .replace("{{KAPASITAS_MAKSIMUM}}", "50")
        .replace("{{RASIO_PASS2}}", "0.25")
        .replace("{{BLOK_BRANDING}}", "")
    )
    assert tomllib.loads(filled)["data_root"] == data_root


def test_the_template_never_hard_codes_an_organisation() -> None:
    """This repository is public and the tool is general purpose.

    A letterhead arrives at build time, from a directory outside the repository.
    """
    for path in PACKAGING.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "INTRAMEDIKA" not in text.upper(), f"an organisation name is baked into {path.name}"


# ===========================================================================
# The builder
# ===========================================================================


def test_the_builder_runs_on_a_bare_interpreter(builder: ModuleType) -> None:
    """It must not import the application.

    A bundle is built from a checkout whose dependencies may not be installed, and the
    builder is what installs them. Importing `mom_igd` would make that circular.
    """
    source = (PACKAGING / "build_bundle.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+mom_igd", source, re.MULTILINE)


def test_the_builder_reads_the_real_application_version(builder: ModuleType) -> None:
    from mom_igd.version import APP_VERSION

    assert builder.version() == APP_VERSION


def test_the_builder_refuses_to_write_inside_the_repository(builder: ModuleType) -> None:
    """The staging tree holds model weights and possibly a list of real people."""
    with pytest.raises(SystemExit) as raised:
        builder.main(
            ["--out", str(REPO / "build-output"), "--models-from", str(REPO / "nowhere")]
        )
    assert "inside the repository" in str(raised.value)
    assert not (REPO / "build-output").exists(), "it must refuse before creating anything"


def test_the_builder_targets_the_interpreter_requirements_txt_targets(
    builder: ModuleType,
) -> None:
    assert builder.PYTHON_VERSION.startswith("3.12."), (
        "3.14 has no wheels for the AI stack; the bundle must not ship its installer"
    )


def test_the_builder_verifies_the_archive_it_wrote(builder: ModuleType) -> None:
    """Backslash entry names are the failure this guards.

    PowerShell 5.1's `Compress-Archive` produces them, Explorer forgives them, and
    7-Zip or `unzip` elsewhere can extract one flat file whose name contains a
    backslash. The builder uses `zipfile` and checks the result.
    """
    source = (PACKAGING / "build_bundle.py").read_text(encoding="utf-8")
    assert "testzip()" in source, "CRC of the written archive is never checked"
    assert 'if any("\\\\" in name for name in names)' in source


def test_build_machine_traces_are_rejected_from_the_archive(builder: ModuleType) -> None:
    for fragment in ("/.venv/", "__pycache__", "/config/local.toml"):
        assert fragment in builder.FORBIDDEN_FRAGMENTS


# ===========================================================================
# The guide keeps its promises modest
# ===========================================================================


@pytest.fixture(scope="module")
def guide() -> str:
    return (BUNDLE / "PANDUAN.md").read_text(encoding="utf-8")


def test_the_guide_says_the_output_is_a_draft(guide: str) -> None:
    assert "DRAF" in guide
    assert "draf" in guide.lower()


def test_the_guide_says_speakers_are_not_identified(guide: str) -> None:
    """A roster of twenty-two people does not make the transcript name anybody.

    This is the single most likely misunderstanding, and it has already been asked
    once: segments read `UNASSIGNED` however many participants are enrolled.
    """
    assert "UNASSIGNED" in guide


def test_the_guide_claims_no_accuracy_figure(guide: str) -> None:
    """Accuracy needs a reference transcript, and no reference transcript exists."""
    assert "belum pernah diukur" in guide
    assert not re.search(r"\b(akurasi|ketepatan)\w*\s+\d+([.,]\d+)?\s*%", guide.lower()), (
        "the guide states an accuracy percentage, which has never been measured"
    )


def test_the_guide_warns_before_the_bundle_is_forwarded(guide: str) -> None:
    """A bundle may carry a participant seed file, which is personal data."""
    assert "participants.local.toml" in guide
    assert "di dalam perusahaan saja" in guide


def test_the_guide_says_where_the_finished_documents_go(guide: str) -> None:
    assert "exports" in guide
