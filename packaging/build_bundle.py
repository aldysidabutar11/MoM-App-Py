"""Build the offline installation bundle: one ZIP that installs without a network.

Why this exists
---------------
The documented path in `README.md` needs the internet twice -- once for `pip install`
and once for `asr provision`, which is ~4.3 GiB per machine. For one developer that is
fine. For a team of ten it is 43 GiB of downloads, ten chances to end up on a different
dependency resolution, and ten machines where the interpreter might be the Microsoft
Store shim. This produces a single artefact that carries the wheels, the models and the
official Python installer, so a colleague extracts it, double-clicks one file, and has a
working installation with no network at all.

What it deliberately does not contain
-------------------------------------
The organisation's people and the organisation's letterhead. Both are supplied at build
time with `--participants` and `--branding-from`; neither is in this repository, because
neither belongs to a general-purpose tool -- and this repository is public. A bundle
built without them installs perfectly well: the participant directory simply starts
empty and the exported documents carry no logo.

Model weights are not in this repository either, for the reasons in `.gitignore`. They
come from a model store that `asr verify` has already passed -- pass it with
`--models-from`. That argument is required rather than derived from configuration on
purpose: a release tool should not guess which model store it is shipping, and the one
mistake worth preventing is shipping from a data root that also holds real meetings.

Network use
-----------
Building needs the internet, for `pip wheel` and for the Python installer. Installing
from the result does not. That asymmetry is the whole point.

Usage
-----
    .venv\\Scripts\\python.exe packaging\\build_bundle.py ^
        --out D:\\MoM-IGD-Rilis ^
        --models-from D:\\MoM-IGD-Data\\models ^
        --branding-from D:\\MoM-IGD-Data\\branding ^
        --participants config\\participants.local.toml ^
        --organisation "ACME" --logo acme.png ^
        --capacity 25 --pass2-ratio 1.0
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLE_SRC = REPO / "packaging" / "bundle"

# Must match the interpreter `requirements.txt` targets. Not 3.14: the AI wheels do not
# exist for it. Not the Store distribution: its filesystem redirection breaks native
# module loading, and both ctranslate2 and llama.cpp are native.
PYTHON_VERSION = "3.12.10"
PYTHON_INSTALLER = f"python-{PYTHON_VERSION}-amd64.exe"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_INSTALLER}"

BUNDLE_NAME = "MoM-IGD-Offline"

# Entries the archive must contain, checked after it is written rather than before.
# The question is what the recipient gets, not what the staging directory held.
REQUIRED_ENTRIES = [
    "1-PASANG.bat",
    "2-JALANKAN.bat",
    "3-PERIKSA.bat",
    "PANDUAN.md",
    "scripts/pasang.ps1",
    "scripts/jalankan.ps1",
    "scripts/periksa.ps1",
    "bahan/local.toml.templat",
    "bahan/models/installed.json",
    "app/requirements.txt",
    "app/requirements-dev.txt",
    "app/mom_igd/__main__.py",
    "app/mom_igd/shell/web/index.html",
]

# Traces of the machine that built the bundle. Any of them means the staging tree was
# reused after an install was run inside it.
FORBIDDEN_FRAGMENTS = ("/.venv/", "__pycache__", "/config/local.toml", "/.pytest_cache/")


def say(message: str) -> None:
    print(message, flush=True)


def step(number: int, total: int, title: str) -> None:
    print(f"\n[{number}/{total}] {title}", flush=True)


def die(message: str) -> None:
    raise SystemExit(f"\nBUILD FAILED: {message}")


# --------------------------------------------------------------------------- sources


def export_source(app_dir: Path) -> int:
    """Copy the tracked source at HEAD into the bundle.

    `git archive` rather than a directory copy: it takes exactly what is committed, so
    a stray `.venv`, a local `config/local.toml` or a `__pycache__` in the working tree
    cannot reach a colleague's machine. It also means a bundle is reproducible from a
    commit hash.
    """
    app_dir.mkdir(parents=True, exist_ok=True)
    tar = app_dir.parent / "_source.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", "-o", str(tar), "HEAD"],
        cwd=str(REPO),
        check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(app_dir), "-f", str(tar)], check=True)
    tar.unlink()
    return sum(1 for p in app_dir.rglob("*") if p.is_file())


def build_wheelhouse(wheels: Path) -> int:
    """Build a wheel for every dependency, including the ones published only as sdists.

    `pip download --only-binary=:all:` fails here: `proxy-tools`, a transitive
    dependency of pywebview, ships no wheel. `pip wheel` builds one, so the installing
    machine needs no compiler and no build isolation -- which it could not have anyway,
    since build isolation would want to fetch its own build backend over the network.

    Only the two requirements files are built. Adding `pip setuptools wheel` on top is
    tempting -- "so a repair install works offline" -- but those three are unpinned, so
    every build would fetch whatever is newest and two bundles cut from the same commit
    would differ. They are also unnecessary: `python -m venv` gets pip and setuptools
    from ensurepip without a network, `setuptools` is pinned in `requirements.txt`
    anyway, and nothing here builds from source, so `wheel` is never invoked.

    Both files go into ONE pip invocation rather than two. Run separately, the resolver
    sees only one set of pins at a time, so a package pinned in the second file but
    merely implied by the first is resolved twice -- once to the pin and once to
    whatever is newest. That is not hypothetical: `onnxruntime` requires `packaging`,
    which `requirements-dev.txt` pins and `requirements.txt` does not name, and two
    passes produced a wheelhouse holding both 26.2 and 26.3.
    """
    wheels.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel",
            "-r", str(REPO / "requirements.txt"),
            "-r", str(REPO / "requirements-dev.txt"),
            "-w", str(wheels),
            "--disable-pip-version-check",
        ],
        check=True,
    )
    sdists = [p.name for p in wheels.iterdir() if p.suffix != ".whl"]
    if sdists:
        die(f"not every dependency became a wheel: {sdists}")

    # Two versions of one distribution means something unpinned got in. `pip install -r`
    # would still pick the pinned one, so this never breaks an install -- it just leaves
    # a wheelhouse whose contents nobody can predict, which is the opposite of the point.
    seen: dict[str, str] = {}
    for wheel in wheels.glob("*.whl"):
        name = wheel.name.split("-")[0].lower().replace("_", "-")
        if name in seen:
            die(
                f"two versions of {name} in the wheelhouse ({seen[name]}, {wheel.name}). "
                "Something unpinned was requested; the wheelhouse must be exactly the "
                "pinned closure."
            )
        seen[name] = wheel.name
    return len(seen)


def fetch_python_installer(vendor: Path, supplied: Path | None, skip: bool) -> Path | None:
    if skip:
        say("  skipped -- the bundle will refuse to install where Python 3.12 is absent")
        return None
    target = vendor / PYTHON_INSTALLER
    if supplied is not None:
        if not supplied.is_file():
            die(f"--python-installer does not exist: {supplied}")
        shutil.copy2(supplied, target)
        say(f"  copied from {supplied}")
    else:
        say(f"  downloading {PYTHON_URL}")
        with urllib.request.urlopen(PYTHON_URL) as response, target.open("wb") as out:
            shutil.copyfileobj(response, out)
    digest = sha256(target)
    say(f"  {target.name}  {target.stat().st_size / 1024**2:.1f} MB")
    say(f"  sha256 {digest}")
    return target


def copy_materials(
    bahan: Path,
    models_from: Path,
    branding_from: Path | None,
    participants: Path | None,
) -> None:
    if not (models_from / "installed.json").is_file():
        die(
            f"no installed.json under {models_from} -- that is a model store which has "
            "never been provisioned. Point --models-from at <data_root>\\models on a "
            "machine where `asr verify` passes."
        )
    shutil.copytree(models_from, bahan / "models")
    size = sum(p.stat().st_size for p in (bahan / "models").rglob("*") if p.is_file())
    say(f"  models      {size / 1024**3:.2f} GB")

    if branding_from is not None and branding_from.is_dir():
        shutil.copytree(branding_from, bahan / "branding")
        say(f"  branding    {len(list((bahan / 'branding').iterdir()))} file(s)")
    else:
        say("  branding    none -- exported documents will carry no logo")

    if participants is not None:
        if not participants.is_file():
            die(f"--participants does not exist: {participants}")
        shutil.copy2(participants, bahan / "participants.local.toml")
        say("  participants  included -- SEE THE WARNING AT THE END")
    else:
        say("  participants  none -- the directory starts empty and is filled in the app")


def write_config_template(bahan: Path, args: argparse.Namespace) -> None:
    """Fill everything in the template except the data root.

    The data root is the one value that cannot be known here: it is chosen on the
    machine being installed, so `pasang.ps1` substitutes `{{DATA_ROOT}}` at install
    time. Everything else is organisation policy and is decided once, here.
    """
    template = (BUNDLE_SRC / "bahan" / "local.toml.templat").read_text(encoding="utf-8")

    if args.organisation:
        branding = [
            "[mom.document]",
            "# Kop pada notulen yang diekspor. Hanya tampilan: tidak ada yang berubah",
            "# pada apa yang diambil dari rapat, apa yang diverifikasi, atau apa yang",
            '# disimpan. Tulisan "DRAF" tepat di bawah kop memang sengaja tidak bisa',
            "# dimatikan lewat konfigurasi.",
            f'organisation = "{args.organisation}"',
            f'organisation_subtitle = "{args.organisation_subtitle}"',
            "",
            "# Nama berkas di dalam <data_root>/branding. Bukan path.",
            "#",
            "# Pakai gambar berlatar transparan. Gambar dengan latar pekat yang ikut",
            "# tercetak akan muncul sebagai pita gelap di kepala halaman putih.",
            f'logo_filename = "{args.logo}"',
        ]
    else:
        branding = [
            "# [mom.document]",
            "# # Kop pada notulen yang diekspor. Kosong berarti tanpa kop.",
            '# organisation = ""',
            '# organisation_subtitle = ""',
            "# # Nama berkas di dalam <data_root>/branding. Bukan path.",
            '# logo_filename = ""',
        ]

    filled = (
        template.replace("{{KAPASITAS}}", str(args.capacity))
        .replace("{{KAPASITAS_MAKSIMUM}}", str(args.max_capacity))
        .replace("{{RASIO_PASS2}}", str(args.pass2_ratio))
        .replace("{{BLOK_BRANDING}}", "\n".join(branding))
    )
    if "{{" in filled.replace("{{DATA_ROOT}}", ""):
        die("a placeholder in local.toml.templat was not filled")
    (bahan / "local.toml.templat").write_text(filled, encoding="utf-8")


# --------------------------------------------------------------------------- archive


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_archive(stage: Path, zip_path: Path) -> None:
    """Write the ZIP with entry names the specification allows.

    .NET Framework's `ZipFile.CreateFromDirectory`, which is what PowerShell 5.1's
    `Compress-Archive` uses, writes Windows separators into entry names --
    `MoM-IGD-Offline\\1-PASANG.bat`. Explorer forgives that, which is exactly why it
    survives testing; 7-Zip and `unzip` elsewhere can extract one flat file whose name
    contains a backslash. `zipfile` writes `/`.

    compresslevel 1 because 4.3 GiB of the payload is quantised model weights that do
    not compress. Level 6 over them costs minutes and saves almost nothing; the wheels
    and the source, which do compress, get most of the benefit at level 1 anyway.
    """
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    say(f"  {len(files)} files")
    with zipfile.ZipFile(
        zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=1
    ) as archive:
        for path in files:
            archive.write(path, arcname=path.relative_to(stage).as_posix())


def verify_archive(zip_path: Path) -> int:
    with zipfile.ZipFile(zip_path) as archive:
        broken = archive.testzip()
        if broken is not None:
            die(f"CRC check failed on {broken}")
        names = archive.namelist()

    if any("\\" in name for name in names):
        die("entry names contain backslashes")

    missing = [e for e in REQUIRED_ENTRIES if f"{BUNDLE_NAME}/{e}" not in names]
    if missing:
        die(f"missing from the archive: {missing}")

    leaked = sorted(
        {name for name in names for bad in FORBIDDEN_FRAGMENTS if bad in "/" + name}
    )
    if leaked:
        die(f"build-machine traces reached the archive: {leaked[:5]}")

    return len(names)


# --------------------------------------------------------------------------- driver


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out", type=Path, required=True,
        help="Directory for the staging tree and the ZIP. Must be outside the repository.",
    )
    parser.add_argument(
        "--models-from", type=Path, required=True,
        help="A provisioned model store, normally <data_root>\\models. Required rather "
             "than derived: a release tool should not guess what it is shipping.",
    )
    parser.add_argument("--branding-from", type=Path, default=None,
                        help="Directory holding the letterhead image. Optional.")
    parser.add_argument("--participants", type=Path, default=None,
                        help="Participant seed file. Optional, and personal data.")
    parser.add_argument("--organisation", default="",
                        help="Letterhead name. Empty leaves the branding block commented out.")
    parser.add_argument("--organisation-subtitle", default="Minutes of Meeting")
    parser.add_argument("--logo", default="",
                        help="Bare filename inside <data_root>/branding.")
    parser.add_argument("--capacity", type=int, default=9,
                        help="Seats on a new meeting's roster (default: the shipped 9).")
    parser.add_argument("--max-capacity", type=int, default=50,
                        help="Safety ceiling for that number (default: the shipped 50).")
    parser.add_argument("--pass2-ratio", type=float, default=0.25,
                        help="Second-pass budget (default: the shipped 0.25; 1.0 is more "
                             "accurate and roughly twice as slow).")
    parser.add_argument("--python-installer", type=Path, default=None,
                        help="Local copy of the official installer instead of downloading.")
    parser.add_argument("--no-python-installer", action="store_true",
                        help="Omit it. The bundle then requires Python 3.12 to be present.")
    args = parser.parse_args(argv)

    if args.organisation and not args.logo:
        say("note: --organisation without --logo -- the letterhead will be text only")
    if args.logo and not args.branding_from:
        die("--logo names a file but --branding-from was not given, so no image ships")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    out = args.out.resolve()
    if out == REPO or REPO in out.parents:
        die(
            f"--out is inside the repository ({out}). Build output must live outside it: "
            "the staging tree holds model weights and possibly a participant list, and "
            "neither may ever be committed."
        )

    stage = out / "stage"
    bundle = stage / BUNDLE_NAME
    if stage.exists():
        shutil.rmtree(stage)
    bundle.mkdir(parents=True)

    total = 7

    step(1, total, "Bundle scripts and guide")
    for item in BUNDLE_SRC.iterdir():
        if item.name == "bahan":
            continue  # the template is written filled in, in step 5
        if item.is_dir():
            shutil.copytree(item, bundle / item.name)
        else:
            shutil.copy2(item, bundle / item.name)
    (bundle / "bahan").mkdir()
    say(f"  {sum(1 for p in bundle.rglob('*') if p.is_file())} files")

    step(2, total, "Application source at HEAD")
    say(f"  {export_source(bundle / 'app')} files")

    step(3, total, "Wheelhouse (needs the network)")
    say(f"  {build_wheelhouse(bundle / 'vendor' / 'wheels')} wheels")

    step(4, total, "Official Python installer")
    fetch_python_installer(
        bundle / "vendor", args.python_installer, args.no_python_installer
    )

    step(5, total, "Models, branding, participants, configuration")
    copy_materials(bundle / "bahan", args.models_from, args.branding_from, args.participants)
    write_config_template(bundle / "bahan", args)

    step(6, total, "Archive")
    zip_path = out / f"{BUNDLE_NAME}-v{version()}.zip"
    if zip_path.exists():
        zip_path.unlink()
    write_archive(stage, zip_path)

    step(7, total, "Verify the written archive")
    entries = verify_archive(zip_path)
    say(f"  {entries} entries, CRC intact, no build-machine traces")

    print()
    print("=" * 74)
    print(f"  {zip_path}")
    print(f"  {zip_path.stat().st_size / 1024**3:.2f} GB")
    print(f"  sha256 {sha256(zip_path)}")
    print("=" * 74)
    if args.participants:
        print()
        print("  This archive contains a participant seed file with real names.")
        print("  Distribute it inside the organisation only. To share it further,")
        print("  delete bahan/participants.local.toml first -- installation is")
        print("  unaffected, the directory simply starts empty.")
    return 0


def version() -> str:
    """The application version, read without importing the package.

    Importing `mom_igd` would work, but this script must stay runnable from a plain
    interpreter in a checkout whose dependencies are not installed -- the same rule
    `doctor` follows, and for the same reason.
    """
    text = (REPO / "mom_igd" / "version.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("APP_VERSION"):
            return line.split("=", 1)[1].strip().strip("\"'")
    die("could not read APP_VERSION from mom_igd/version.py")
    return ""  # unreachable; keeps the type checker happy


if __name__ == "__main__":
    raise SystemExit(main())
