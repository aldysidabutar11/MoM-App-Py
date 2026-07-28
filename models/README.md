# `models/` — declaration only, never binaries

This directory holds **`registry.json`**: a versioned, Git-tracked *declaration*
of the model artefacts the application is permitted to load.

## What lives where

| Thing | Location | Committed? |
|---|---|---|
| Model declaration (`registry.json`) | this directory | **Yes** — it is reviewable metadata |
| Model binaries (`.gguf`, `.onnx`, `.bin`, …) | `<data_root>/models`, default `D:\MoM-IGD-Data\models` | **Never** |

`.gitignore` in this directory ignores everything except `registry.json`,
`README.md` and itself. `.gitattributes` at the repository root additionally
marks every model file extension as `binary`, so no line-ending translation can
corrupt an artefact or invalidate its SHA-256.

## Current state (Phase 1)

The registry is **empty, and that is correct**. No ASR, diarization,
speaker-embedding or LLM provider has been selected. The selection is deferred to
the Phase 4A benchmark — see
[`docs/adr/0005-ai-provider-selection-deferred-to-phase-4a.md`](../docs/adr/0005-ai-provider-selection-deferred-to-phase-4a.md).

An empty registry validates successfully and produces a **doctor warning**, never
a failure:

```
python -m mom_igd doctor
...
[WARN] model_registry     Registry is valid but declares 0 models
```

## Registry schema (version 1)

```jsonc
{
  "registry_schema_version": 1,
  "description": "...",
  "models": [
    {
      "provider": "asr",                  // vad | asr | diarization | speaker_embedding | llm | text_embedding
      "name": "example-model",
      "version": "1.0.0",
      "path": "asr/example/model.bin",    // relative to <data_root>/models, or absolute
      "sha256": "<64 lower-case hex chars>",
      "size_bytes": 0,
      "license_name": "Apache-2.0",
      "license_url": null,
      "license_requires_acceptance": false,
      "provisioned": false,               // artefact exists locally
      "offline_ready": false,             // verified and usable with no network
      "hardware_profile": "cpu-int8",     // no CUDA profile exists: the device has no NVIDIA GPU
      "source_url": null,                 // recorded for audit; never fetched at runtime
      "phase_introduced": "4",
      "notes": null
    }
  ]
}
```

Validation rules enforced by `mom_igd/registry.py`:

- `provider` must be a known logical slot.
- `hardware_profile` must be a known profile. **There is no CUDA profile** — the
  target device has Intel Iris Xe integrated graphics and no NVIDIA GPU.
- `sha256` must be exactly 64 lower-case hex characters.
- `path` must be an absolute path or a path containing a separator; it may never
  be a remote URL (checked by the same offline endpoint policy as provider
  endpoints).
- `offline_ready: true` requires `provisioned: true`.
- `(provider, name, version)` must be unique.

## Provisioning

Provisioning is a **separate, explicit, one-time online step** that arrives with
the phase that needs the model. It is not implemented in Phase 1 and nothing in
this repository downloads a model. At runtime the application is offline: model
paths are read from this registry and loaded from the local filesystem only.
