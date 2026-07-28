# ADR-0002 — What "offline" means, and how it is enforced

* **Status:** Accepted
* **Phase:** 0 (audited) / 1 (implemented)

## Context

The requirement is to "operate without cloud APIs and without internet during
runtime". Taken literally and without qualification this is unsatisfiable: no
model can exist on a machine without having been transferred there at some point.
So "offline" needed an operational definition, and — more importantly — a
mechanism, because a policy nobody enforces is a comment.

The legacy project on the same machine (`Project APP VTT`) depends on
`google-genai`, a hosted inference API. That is exactly the architecture this
project must not drift into, and drift happens one convenient import at a time.

## Decision

### 1. Three distinct lifecycle phases

| Phase | Network | What happens |
|---|---|---|
| **Provisioning** (one-time, controlled) | allowed | Download dependencies and models, verify SHA-256, freeze into a bundle |
| **Installation** (on the production device) | offline | Install from the bundle; verify checksums |
| **Runtime** (meeting and processing) | **offline, enforced** | No outbound request whatsoever |

If the production device must never be online at all, the bundle can be carried on
physical media. The design supports that without change.

### 2. Offline-ness is enforced at the application level, in three rules

* **Dependency policy** — `offline_policy.CLOUD_SDK_DENYLIST` plus prefix rules
  (`azure-`, `google-cloud-`, …) name the distributions that must never be
  installed. `DEFERRED_HEAVY_DISTRIBUTIONS` separately names the AI/audio
  dependencies that belong to a later phase, so "not yet" is not confused with
  "never". Both are checked against the live environment and asserted by
  `tests/test_offline_policy.py`.
* **Endpoint policy** — a provider endpoint may only be a local filesystem path or
  a loopback URL. Anything else is refused at configuration load. A value with no
  scheme and no path separator (`api.openai.com`, `model.bin`) is **rejected as
  ambiguous rather than guessed at**.
* **Bind policy** — the API binds a loopback address only. `0.0.0.0`, `::`, a LAN
  address or a public address is refused by configuration validation, and again by
  `BackgroundServer` as defence in depth.

Plus a `Host`-header allowlist, so a page on the internet cannot point a DNS name
at `127.0.0.1` and drive this backend through the user's browser. That is also
what makes the claim "Swagger is not exposed outside loopback" *enforceable*
rather than merely true by accident.

### 3. Environment flags are defined now, libraries later

`offline_env_flags()` returns `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`,
`HF_DATASETS_OFFLINE`, `HF_HUB_DISABLE_TELEMETRY`, `DO_NOT_TRACK`,
`MOM_IGD_OFFLINE`. They do nothing in Phase 1 because none of those libraries are
installed. They exist so the worker-spawn path is already correct the day one is.

### 4. Explicitly rejected: a global `socket.socket` monkey-patch

Patching the socket layer process-wide was considered and **rejected**:

* it breaks loopback IPC, which the architecture depends on (API, shell bridge,
  future worker channels);
* its effectiveness depends on import order, so it fails silently and
  unpredictably;
* it converts a design property into a runtime trick, and hides real bugs behind
  a blanket interception;
* it gives false confidence: a subprocess, a native library or a DLL would not be
  covered anyway.

Offline-ness is a property of what is installed and what is configured. That is
what gets enforced.

### 5. Also rejected: dialling out to prove blocking works

No test opens a connection to the internet to demonstrate that it fails. The
property is asserted by construction (no cloud client is installed, no non-local
endpoint is accepted) rather than by generating the traffic it forbids.

### 6. Deferred: operating-system firewall hardening

Per-executable Windows Firewall outbound rules are **Phase 11**. Phase 1 does not
change firewall configuration — that is a system change and out of scope.

### 7. No cloud fallback, ever

There is no degraded mode that reaches a hosted API when a local model is missing.
A missing model is an error with an explanatory message. `runtime_mode` accepts
only `offline`, and `offline = false` is rejected: it is an architectural
invariant, not a toggle.

## Consequences

**Good.** The guarantee is inspectable: read the denylist, read the validators,
read the lock files. It holds for subprocesses because it is about what exists on
disk, not about intercepting calls. It survives import-order changes. Tests can
assert it without any network.

**Bad / accepted.** It does not stop a *deliberately* malicious dependency from
opening a socket — that is what the Phase 11 firewall layer and dependency review
are for. It requires discipline at dependency-review time, which is why the
dependency audit is a test rather than a document.

**Operational.** `httpx` is a **test-only** dependency (required by
`starlette.testclient`). The runtime closure deliberately contains no HTTP client:
the desktop shell's loopback proxy and the headless smoke test both use
`urllib.request` from the standard library.
