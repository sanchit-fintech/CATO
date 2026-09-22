# Scheduler

Schedules share the durable task database. A schedule has an ID, display name,
request, timezone-aware first run, optional interval, next/last run, and enabled
state. Intervals are at least 60 seconds.

The worker claims due schedules transactionally and creates ordinary queued
tasks. Scheduled work therefore uses the same approved roots, tool registry,
budgets, and human approval gates as interactive work. One-time schedules disable
after being claimed; recurring schedules compute their next run and survive
server restarts.

API endpoints are `POST /schedules` and `GET /schedules`. Timestamps must be ISO
8601 with an explicit timezone. Natural-language date parsing is intentionally
not implemented.
