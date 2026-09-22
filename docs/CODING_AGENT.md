# Coding agent foundation

`source_apply_patch` performs one exact text replacement inside an approved root.
It requires explicit approval, enforces a patch-size budget, optionally validates
the inspected SHA-256 hash, and requires the old fragment to occur exactly once.
Writes use a flushed temporary file and atomic replacement while preserving mode.

For Git repositories, a file with pre-existing staged, unstaged, or untracked
changes is refused. Cato never resets the worktree and never touches unrelated
dirty files. A successful patch returns before/after hashes and changed-line
counts. Test and Ruff tools can then validate the result under their independent
approval gates.

This is a safe editing primitive, not yet a fully automatic multi-cycle repair
agent or auto-revert system.
