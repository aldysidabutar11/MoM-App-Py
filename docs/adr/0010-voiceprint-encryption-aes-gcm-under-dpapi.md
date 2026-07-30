# ADR-0010 — Voiceprint encryption: AES-256-GCM under a DPAPI-protected key

* **Status:** Accepted
* **Phase:** 3

## Context

A voiceprint is biometric data. Under Indonesia's UU PDP No. 27/2022 it is *data
pribadi bersifat spesifik*. The realistic threat is not a network attacker — this
application has no network — but **someone obtaining the runtime data directory**: a
stolen laptop, a copied backup, a support archive, a shared machine.

Phase 1 and 2 stored nothing biometric, so this is the first decision about
encryption at rest in the project.

## Decision

**Each voiceprint payload is sealed with AES-256-GCM under a random 256-bit master
key, and that key is protected by Windows DPAPI for the current user.**

### Why authenticated encryption, and why not the alternatives

* **Base64 is not encryption** and must never be presented as such. It is stated
  here explicitly because it is the mistake this kind of code attracts.
* **A raw block cipher without a MAC** would let an attacker who can write to the
  data directory flip bits in a template and have it silently accepted. GCM detects
  that instead of returning plausible-looking garbage.
* **DPAPI per payload** was rejected. It would work, but it gives no versioned
  envelope, no explicit nonce, and no caller-controlled additional data — and the
  additional data is the part that carries most of the security value here.

### The AAD is not optional, and it is what stops the worst attack

Confidentiality alone is insufficient. Without authenticated additional data, an
attacker (or a bug, or a careless restore) could copy Budi's envelope over Siti's:
the bytes would decrypt perfectly, and Phase 6 would then confidently identify
Budi's voice as Siti. That is a *worse* outcome than a decrypt failure, because it
is silent and wrong.

The AAD therefore binds every ciphertext to:

* the `voiceprint_uuid` it was created for,
* the `participant_id` it belongs to,
* the envelope schema version,
* the embedding model's name, **version** and **SHA-256**.

Moving an envelope between participants fails to authenticate. Reusing one after the
model changes also fails — which matters independently, because embeddings from
different models are not comparable and a template that survived a model swap would
produce meaningless similarity scores.

The AAD is built by joining fixed fields with a separator that cannot appear in
them. `json.dumps` was rejected: key ordering and whitespace would then be part of
the security boundary, and a library upgrade that changed either would make every
existing envelope unopenable.

### Nonce discipline

96 bits from the OS CSPRNG, fresh for every seal. Nonce reuse under one key is
catastrophic for GCM — it leaks the XOR of two plaintexts and permits forgery — so
nonces are never derived from a counter that could restart after a crash or a
restore from backup.

### Key handling

* The key is created **only** by an explicit enrollment. `KeyProtector.load()`
  refuses to create one, so importing a module, listing participants or running
  `doctor` can never bring a key into existence.
* A **missing key when voiceprints exist is a hard failure**, not an invitation to
  mint a replacement. A new key cannot decrypt anything, so silently creating one
  would turn a recoverable diagnosis into permanent, invisible data loss.
* `MasterKey.__repr__`, `__str__` and `__format__` all return `<redacted>`, so an
  accidental log line or f-string cannot leak it. Only `.value`-style explicit
  access reaches the bytes.
* `key_id` — a truncated hash of the key, never the key — is recorded in the
  envelope so a wrong-key decrypt fails with a clear diagnosis. **Security does not
  rest on it**: with the header stripped, the GCM tag still rejects a wrong key, and
  a test asserts exactly that.

### DPAPI through `ctypes`, not pywin32

`CryptProtectData` and `CryptUnprotectData` live in `crypt32.dll` and take two flat
structures. Adding a dependency for two calls would grow the offline closure for
nothing. `CRYPTPROTECT_UI_FORBIDDEN` is set, because a dialog from a background call
would hang the application. A fixed entropy string is mixed in as a domain
separator, so a blob protected for this purpose cannot be unwrapped by some other
feature that also calls DPAPI as the same user.

### What this does **not** protect against — stated plainly

* **Anything running as that same Windows user can call `CryptUnprotectData` too.**
  DPAPI defends against another account and against a stolen copy of the file. It
  does not defend against malware already executing as the operator.
* **File deletion does not erase data from an SSD.** `PRAGMA secure_delete` is
  enabled on the connection that deletes voiceprint metadata, and a
  `wal_checkpoint(TRUNCATE)` follows so freed pages do not linger in the `-wal`
  file. Neither reaches the physical NAND: wear levelling and over-provisioning keep
  old copies until the controller reuses them.
* **Backups are not covered.** A copy of the data directory taken before a
  revocation still contains the template, and nothing in this application can reach
  into that copy.

The honest conclusion is that **full-volume encryption (BitLocker) is a Phase 11
requirement, not an optional hardening step**, and that backup and key-escrow policy
must be decided there too. Documenting the limit is the point; hiding it would let
someone believe deletion is stronger than it is.

### What is inside the ciphertext, and what is not

Inside: the normalised centroid, the per-dimension dispersion, the per-sample
embeddings, the sample count, the embedding dimension, the serialisation version,
the preprocessing identity.

Outside, in the database: non-biometric metadata only — status, model name/version/
hash, embedding *dimension* and sample *count* (shape, not content), device
provenance, quality verdict, envelope hash and size. Knowing a template has 192
dimensions reveals nothing about a voice, and Phase 6 needs both to reject a
mismatched model before attempting a decrypt.

No embedding, ciphertext, nonce or key ever reaches an API response, a log line or
the audit trail. Tests assert this over every readable route.

## Consequences

**Good.** A stolen data directory yields no voice templates. Tampering, truncation, a
wrong key, a swapped participant and a changed model are all detected and
distinguished. One new dependency (`cryptography`, prebuilt wheel, no compiler, no
network call) rather than a hand-rolled cipher.

**Bad / accepted.** A key lost with the Windows profile means every voiceprint must
be re-enrolled — deliberately, since the alternative is a recoverable key, which is a
weaker key. Plaintext exists in process memory while an embedding is computed; the
mitigation is a short-lived worker rather than a `del` statement, and Python cannot
guarantee a buffer is wiped. Encryption is scoped to voiceprints in this phase:
meeting audio and transcripts remain unencrypted until Phase 11, and the recording
panel says so rather than letting an operator assume otherwise.
