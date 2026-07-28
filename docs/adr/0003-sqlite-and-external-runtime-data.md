# ADR-0003 — SQLite with WAL, and runtime data outside the repository

* **Status:** Accepted
* **Phase:** 0 (audited) / 1 (implemented)

## Context

Single-device offline desktop application, one user, one writer at a time, with a
requirement to survive a crash mid-recording without losing already-written audio.
Redis, Kafka, MinIO and any vector database are excluded by product decision.

Two things had to be settled: which database, and where runtime data lives.

The second question is not cosmetic. The failure mode it prevents is concrete:
recordings, voiceprints, generated documents or a database accidentally committed
to Git. The development machine also has `core.autocrlf = true` (verified in
Phase 0), which would silently rewrite line endings in any file Git guesses is
text — corrupting FLAC audio, ONNX/GGUF models and SQLite files, and invalidating
every SHA-256 in the audio manifest and the model registry.

## Decision

### Database: SQLite, with pragmas verified rather than assumed

`journal_mode=WAL`, `foreign_keys=ON`, a configured `busy_timeout`, and
`synchronous=NORMAL`. Every connection **reads the pragmas back** and raises if
WAL or foreign keys are not confirmed. Running without WAL would remove the crash
resilience the recording pipeline depends on; running without foreign keys would
let orphaned chunks and stages accumulate silently. Neither is acceptable as a
warning.

An in-memory database therefore cannot be used, since SQLite cannot put one into
WAL. Tests use file-backed databases in temporary directories, which is also more
faithful to production.

### Migrations: versioned, transactional, idempotent, tamper-evident

* Files are `NNNN_name.sql`; versions must be contiguous from 1. A gap or a
  duplicate is an error, not something to sort around.
* Each migration runs inside `BEGIN IMMEDIATE` **together with the row that
  records it**. SQLite makes DDL transactional, so a failure rolls back both — the
  schema version can never advance past a migration that did not fully apply.
* Re-running is a no-op.
* The SHA-256 of each migration is recorded, with line endings normalised before
  hashing so `core.autocrlf` cannot change a checksum. Editing an applied
  migration is detected rather than silently diverging.
* `sqlite3.executescript` is **not used**: it issues an implicit `COMMIT` before
  running, which would defeat the transaction. Statements are split by
  `split_sql_statements`, which understands `--` and `/* */` comments, string
  literals with `''` escapes, `"` / `` ` `` / `[]` quoted identifiers, and
  `CREATE TRIGGER … BEGIN … END` bodies.

**There is no production downgrade path.** A `down` migration here would mean
dropping tables holding recordings metadata, transcripts and approvals — that is a
data-loss tool wearing the costume of a feature. Recovery is restore from
`<data_root>/backups`. The only rollback is the transactional rollback of a
failing migration.

### Runtime data lives outside the repository

Default root `D:\MoM-IGD-Data`, overridable through `MOM_IGD_DATA_DIR` or
configuration — **never hardcoded as the only valid location**. Precedence:
`--data-dir` > `MOM_IGD_DATA_DIR` > `config/default.toml` > built-in default.

```
<data_root>\
├─ db\  recordings\  exports\  logs\  models\  temp\  backups\
```

`mom_igd/paths.py` is the sole owner of runtime paths and rejects: a relative
path, a bare filesystem anchor (`D:\`), the repository itself, anything inside the
repository, and any parent directory that would contain the repository (which would
place runtime data *around* the source tree).

Directories are created **only** by `RuntimePaths.ensure()`, called from an
explicit initialisation path (`db init`, `serve`, `shell`). Importing a module
creates nothing; `doctor` creates nothing. Diagnosing a machine must not change it.

D: was chosen over C: for space (197.3 GB vs 111.8 GB free) and separation from the
OS volume. Both are partitions of the same physical NVMe disk, so this provides
**no I/O isolation and no redundancy** — a point worth stating rather than
implying.

### Repository hygiene

`.gitignore` excludes `.venv`, Python and test caches, coverage output, IDE files,
`.env` and key material, logs, `*.db*`, runtime data directories, every audio
extension, every model extension, voiceprints, exports, evaluation datasets and
temporary files. `.gitattributes` marks every binary format `binary` so no
line-ending translation can touch it, and forces `eol=crlf` for `.bat`/`.cmd`/`.ps1`
and `eol=lf` for `.sh`.

`models/` keeps only `registry.json`, `README.md` and its own `.gitignore`. Model
**binaries** live under `<data_root>/models` and are never committed.

## Consequences

**Good.** Zero-configuration, single-file database with real transactional DDL, so
migrations are genuinely atomic. WAL means a crash can lose at most the last
transaction and cannot corrupt the file. Source and data cannot become entangled,
which makes "never commit a recording" a structural property rather than a habit.
The data root can be relocated without touching the database, because
`recordings.relative_dir` and chunk filenames are stored relative.

**Bad / accepted.** SQLite allows one writer at a time — fine for a single-device
application, and the reason the API is the sole writer of workflow state. WAL adds
`-wal` and `-shm` sidecar files that backup procedures must include. Restoring a
backup taken at a different schema version needs care; Phase 11 owns that runbook.

**Not yet implemented.** Encryption at rest, backup/restore automation and
**retention enforcement**. Nothing is deleted automatically today.
