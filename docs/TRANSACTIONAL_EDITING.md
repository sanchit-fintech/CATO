# Transactional editing

Cato 0.13 validates an entire patch set before modifying any file. Supported
operations are exact replacement, insertion before/after an exact anchor, and
new text-file creation. Every existing target requires its inspected SHA-256
hash and must not contain pre-existing Git changes.

Python, JSON, and TOML outputs are parsed before application. Patch file count,
serialized patch bytes, and checkpoint bytes are bounded. `--dry-run` semantics
return hashes for the proposed outputs without changing the workspace.

Before application, Cato stores user-only checkpoint artifacts below
`.cato/artifacts/TASK_ID/checkpoint`. Writes use same-directory temporary files,
`fsync`, mode preservation, and atomic replacement. Transaction metadata records
before/after hashes and ownership.

Rollback restores only files whose current hash still matches Cato's produced
hash. If a user or another process edits a target afterward, rollback stops with
`rollback_conflict` and does not overwrite that work.
