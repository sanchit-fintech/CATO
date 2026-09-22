# Database schema and migrations

Cato deliberately uses separate SQLite files for long-term memory and task
orchestration. This prevents task/event write traffic and retention policy from
coupling to user memory backups.

The task database contains `tasks`, `task_steps`, `task_events`, `workers`, and
`scheduled_jobs`. Common status/session/event lookups are indexed. Task events use
monotonic SQLite integer IDs for reconnect pagination. Schema version is tracked
with `PRAGMA user_version`; migrations reject databases created by newer Cato.

The memory database contains `memories` plus an FTS5 index maintained by triggers.
Version 2 upgrades existing version-1 databases transactionally and rebuilds the
index without deleting memories.
