# Migrating from 0.11 to 0.12

No manual migration command is required. On first open, the task database adds
attempt, idempotency, and archive tables through transactional schema upgrades.
Existing tasks, steps, events, workers, and schedules are retained.

Approvals now use `CATO_APPROVAL_DATABASE_PATH`, defaulting to
`.cato/cato-approvals.sqlite3`. Previously pending in-memory approvals cannot be
recovered, but newly created non-secret approvals survive restart.

Background execution now uses spawned processes. Ensure the user running Cato can
create local child processes. Set `CATO_API_TOKEN` before configuring any
non-loopback host. Existing loopback installations remain compatible without a
token.
