# ADR-0007 — Chunking, checksums and crash recovery

* **Status:** Accepted
* **Phase:** 2

## Context

A meeting happens once. Any failure that loses audio loses it permanently, so the
capture path has to survive a process kill, a power loss, a full disk, a microphone
being unplugged and a writer that cannot keep up — and it has to be *honest* about
what it lost when it does lose something.

A single continuous WAV was the obvious starting point and fails on all counts: the
header carries the data length, so a crash leaves an invalid file; the format is
capped at 4 GiB; and one bit of corruption endangers the whole meeting.

## Decision

### Chunked capture

Audio is written as rotating chunks, 30 s by default and configurable 10–120 s.
Shorter chunks lose less to a crash but multiply file count; longer ones approach
the WAV size limit and lose more when one file is damaged. Rotation happens on an
exact frame boundary — a block straddling a boundary is split precisely — so every
chunk holds exactly `frames_per_chunk` frames and the recorded frame ranges are
contiguous with no rounding.

### Durability order

1. callback → bounded queue
2. writer appends raw PCM to `chunk_NNNNNN.pcm.part`
3. `chunk_NNNNNN.meta.json` written **before any audio**
4. flush + `fsync` the partial at the boundary
5. build a valid WAV at `chunk_NNNNNN.wav.tmp`
6. `fsync` it, then SHA-256 it **from disk**
7. `os.replace` into place — atomic, same volume
8. database row + manifest line
9. remove the partial and its metadata

**The partial is raw PCM, not a WAV.** A WAV needs its header patched with the final
length, so a crash mid-recording would leave a file no tool can read. Raw PCM has no
header to patch: recovery reads whole frames from the front and wraps them in a
fresh header.

**The metadata sidecar comes first.** Without it, a partial is an anonymous blob —
sample rate, channel count and format are all unknowable, and the audio is
unrecoverable in practice. Writing it before the first byte of audio costs one
`fsync` per chunk and makes the difference between salvage and loss.

**The hash is computed from disk, after `fsync`.** Hashing the in-memory buffer would
certify what we *meant* to write; hashing the file certifies what is actually there.

**The partial is removed only after the final file is proven present with the right
size.** Step 8 before step 9 means a crash can leave the manifest ahead of the
database — the recoverable direction. A database row pointing at a chunk with no
manifest record would not be.

### Consequences that fall out of the ordering

* **Any `.wav` that exists is complete.** A crash can leave a `.part` or a `.tmp`,
  never a half-written `.wav`.
* **The writer refuses to overwrite an existing final chunk.** A sequence collision
  discards the new data rather than destroying audio that is already verified.

### Manifest: JSON Lines, plus a chain hash

`manifest.jsonl` is append-only, one self-contained JSON object per line, flushed and
`fsync`ed. A crash can therefore only damage the final line, which is detectable and
discardable without losing anything earlier. A single JSON document would have to be
rewritten on every chunk, and a crash during that rewrite would destroy the record of
every chunk before it.

`manifest.json`, written at finalisation, adds a hash chain over the ordered chunk
list. A tampered chunk is caught by its own SHA-256; a manifest *edited to match* a
tampered chunk is caught by the chain.

The manifest is **authoritative** and the database mirrors it. It is written next to
the audio, by the thread that wrote the audio, before the matching database
transaction commits. `audio verify` compares the two and reports a divergence rather
than reconciling it silently.

### Loss accounting

The bounded queue (default ~5 s) makes back-pressure explicit. A disk hiccup costs
queue depth; only a sustained stall costs audio. When it does, the dropped frame
count is recorded in the chunk record, written to the manifest as an unintentional
`gap`, stored in the database, surfaced in the UI in red, and audited.

**No silence is ever fabricated to fill a gap.** A recording with a known 40 ms hole
is useful; one with an invisible hole is not — every downstream timestamp would be
silently wrong.

A pause is the same mechanism used deliberately: the stream is stopped, the open
chunk is finalised, and an *intentional* gap is recorded. Resume opens a new chunk.
The missing interval is visibly absent from the timeline rather than stitched over.

### Recovery

Recover only whole frames; discard and count a trailing fragment. Never overwrite a
valid final chunk. Never delete evidence — anything ambiguous or corrupt moves to
`quarantine/` with a reason file. Idempotent: a second pass changes nothing. A
partial with no metadata and no known fallback format is quarantined rather than
guessed at.

## Consequences

**Good.** A killed process loses at most the frames still in the queue. Corruption is
detected, localised to one chunk, and provable. Recovery is testable end to end
against a deterministic source, so "byte-exact" is an assertion rather than a claim.

**Bad / accepted.** Each chunk costs a read-back and a hash: measured at 0.38 ms mean
write and 33 ms maximum finalise, which is invisible against a 30 s chunk. Two
`fsync`s per chunk. Disk holds the partial and the finished WAV simultaneously for a
moment, so peak usage is briefly ~1.5× one chunk. Directory `fsync` is not performed
on Windows, where it is unsupported — the atomic rename plus the file `fsync` is what
the guarantee rests on.
