# ADR-0001 — Native Windows is the production runtime

* **Status:** Accepted
* **Phase:** 0 (audited) / 1 (implemented)
* **Supersedes:** the preliminary suggestion that Docker "may be used if justified"

## Context

The application records meetings from a physical microphone and then runs heavy
CPU inference, on one laptop: Windows 11, Intel Core i7-1260P, 16 GB RAM, Intel
Iris Xe, no NVIDIA GPU. Candidate runtimes were native Windows, hybrid
native + local service, and Docker-based.

Measured on the production device during the Phase 0 audit:

* `docker info` → `MemTotal = 8,182,722,560` bytes (**≈7.6 GiB**) reserved for the
  WSL2 VM, on a machine with 16 GB total and **≈4.1 GB actually free** during a
  normal desktop session.
* Docker 29.6.2, Compose v5.3.1 and WSL2 (Ubuntu-22.04) were installed and running.
* No `.wslconfig` existed, so WSL2 was using its default ceiling of about half of
  system RAM.
* No MSVC, no CMake, no Visual Studio/Build Tools, no Windows SDK, no Rust.
* .NET 8/9/10 **runtimes** present but **no SDK**.
* Python 3.14 (official) and Python 3.11 (**Microsoft Store shim**) present;
  **no Python 3.12**.

## Decision

**The production runtime is native Windows, as several cooperating processes.**

1. **Docker Desktop and WSL2 are not production dependencies.** Docker may be used
   for optional CI/test isolation in future, never on the runtime path, and never
   anywhere near the audio path.
2. The application **never** starts, stops or configures Docker, WSL, a browser or
   any other user process, and **never** creates or modifies `.wslconfig`. It
   reports their presence and memory use as *information* and leaves the decision
   to the operator.
3. **Electron and Tauri are rejected** for the desktop shell. Tauri needs Rust +
   MSVC, neither installed. Electron would cost 300–500 MB of RAM on a machine
   that has ~4 GB free. The shell is **pywebview over WebView2** — already present
   on the device, needs no compiler, and gives offline print-to-PDF for the
   Phase 10 exporter at no extra cost.
4. **Python 3.12 from python.org, installed per-user**, is the target interpreter.
   Not 3.14 (the AI wheels needed from Phase 4 are unavailable), and not the
   Microsoft Store shim (filesystem redirection and app-container sandboxing break
   PyInstaller packaging and native library loading).
5. Because no C/C++ toolchain exists, the architecture **prefers prebuilt wheels
   and binaries** over anything that must be compiled. This is what removed
   `whisper.cpp + OpenVINO` as the ASR *foundation* (see ADR-0005).
6. The **internal laptop microphone is for early development only.** A USB
   omnidirectional conference microphone is required before Phase 2 production
   acceptance.

## Consequences

**Good.** Direct WASAPI access, with no virtualisation layer between the
application and the audio device. Roughly 7.6 GiB of RAM is not pre-committed to a
Linux VM. Far fewer moving parts on a single-user device: no daemon that must be
running before the application can start. Installation is a per-user install with
no administrator rights and no container runtime.

**Bad / accepted.** The application is Windows-only, which is fine — it is the
stated target. Development on another OS can run the test suite but not capture
audio. Reproducibility rests on pinned wheels rather than an image digest; a
hash-pinned offline wheelhouse in Phase 11 closes that gap.

**Operational.** Docker Desktop and browsers should be closed during recording and
processing. The doctor reports their memory use so the operator can decide, but the
application will not act on their behalf.

## Alternatives rejected

| Option | Why not |
|---|---|
| Docker-based runtime | Containers cannot reach WASAPI; ≈7.6 GiB VM reservation on a 16 GB machine; Intel GPU passthrough to WSL2 adds fragility; requires a running daemon |
| Hybrid native capture + containerised workers | Keeps every Docker memory cost while adding an IPC boundary and a second packaging story |
| Electron shell | 300–500 MB RAM overhead; needs an npm build pipeline, explicitly out of scope |
| Tauri shell | Requires Rust + MSVC; neither installed, and installing a full toolchain is unjustified for one window |
| Python 3.11 Store shim | Filesystem redirection and sandboxing break packaging and native loading |
| Python 3.14 | Too new for the torch / OpenVINO / ASR wheels required from Phase 4 |
