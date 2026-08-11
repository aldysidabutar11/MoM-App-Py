# `packaging/` — the offline installation bundle

One ZIP that installs the whole application on a Windows laptop **with no network at
all**: the wheels, the models and the official Python installer travel inside it.

The path in the top-level [`README.md`](../README.md) needs the internet twice — `pip
install`, then `asr provision` at ~4.3 GiB per machine. That is fine for one developer.
For a team it is tens of gigabytes of downloads, one chance per machine to resolve
dependencies differently, and one chance per machine to end up on the Microsoft Store
interpreter, whose filesystem redirection breaks native module loading.

## Build one

Building **does** need the network — for `pip wheel` and the Python installer. Installing
from the result does not. That asymmetry is the entire point.

```powershell
.\.venv\Scripts\python.exe packaging\build_bundle.py `
    --out D:\MoM-IGD-Rilis `
    --models-from D:\MoM-IGD-Data\models
```

That produces a generic bundle: no letterhead, and an empty participant directory. Add
the organisation's own material when there is some:

```powershell
.\.venv\Scripts\python.exe packaging\build_bundle.py `
    --out D:\MoM-IGD-Rilis `
    --models-from D:\MoM-IGD-Data\models `
    --branding-from D:\MoM-IGD-Data\branding `
    --participants config\participants.local.toml `
    --organisation "ACME" --logo acme.png `
    --capacity 25 --pass2-ratio 1.0
```

`--out` must be outside the repository, and the builder refuses if it is not: the
staging tree holds model weights and possibly a list of real people, and neither may
ever be committed.

`--models-from` is required rather than derived from configuration. A release tool
should not guess which model store it is shipping, and the one mistake worth preventing
is shipping from a data root that also holds real meetings.

## What the recipient gets

```
MoM-IGD-Offline/
  PANDUAN.md          the operator guide, in Indonesian
  1-PASANG.bat        install: once, ~5 minutes, no network
  2-JALANKAN.bat      run
  3-PERIKSA.bat       diagnose; changes nothing
  app/                the source at HEAD, via `git archive`
  vendor/wheels/      every dependency, as a wheel
  vendor/python-*.exe the official installer, offered only if 3.12 is absent
  bahan/              models, and optionally branding and a participant seed
  scripts/            what the three .bat files actually run
```

`1-PASANG.bat` asks two questions — may Python 3.12 be installed, and where should the
data live — and does everything else itself. It changes no system configuration: no
PATH, no registry, no firewall, no audio device settings. The virtual environment lives
inside the extracted folder.

## What is deliberately not in this directory

**The organisation's people and the organisation's letterhead.** Both are build-time
inputs. Neither is in this repository, because neither belongs to a general-purpose tool
— and this repository is public. A bundle built without them installs perfectly well.

**Model weights**, for the reasons in `.gitignore`. They come from a model store that
`asr verify` has already passed.

## Editing the bundle scripts

`packaging/bundle/` is copied into the ZIP almost verbatim, so a change there ships to
every future bundle. `tests/test_offline_bundle_packaging.py` asserts the properties
that matter and cannot be checked by reading the scripts casually:

- `pasang.ps1` never fetches anything except the Python installer it was told to offer,
  and installs dependencies with `--no-index` so a missing wheel fails loudly instead of
  quietly reaching PyPI;
- the only thing any bundle script deletes is the virtual environment it created;
- every placeholder in `local.toml.templat` is filled by either the builder or the
  installer, and no other placeholder exists;
- `PANDUAN.md` keeps saying the output is a draft, keeps saying speakers are not
  identified, and claims no accuracy figure.

Those are the four ways this bundle could quietly become untrue.
