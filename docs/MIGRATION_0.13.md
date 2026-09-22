# Migrating from 0.12 to 0.13

Transactional editing introduces no destructive database migration. New coding
artifacts are created lazily inside the selected workspace under `.cato/artifacts`.
Add that directory to project ignore rules if desired. Existing 0.10–0.12 task,
event, approval, schedule, and memory databases remain compatible.
