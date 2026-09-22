# Cato architecture

Cato is a synchronous, local-first agent runtime. A model proposes one typed
action at a time; the runtime validates it against the registry, checks approval
policy, executes it, and returns a structured observation. It never executes
model-produced shell text directly.

Each request creates an `AgentTask` with stable task and request IDs. Its trace
records accepted requests, tool outcomes, approval transitions, durations, and
terminal errors. Work is bounded by iteration count, wall-clock duration, and a
repeated-equivalent-action guard. Cancellation is cooperative between actions.

Session history is bounded and intentionally excludes full tool payloads.
Long-term facts and preferences use a versioned SQLite database. Potential
credentials and private keys are refused; SQLite storage itself is not encrypted.

All filesystem and project tools resolve paths through approved roots and reject
traversal and symlink escapes. Command execution uses argument arrays, never a
shell, and remains narrowly allowlisted. Native macOS actions use the same tool
registry and approval gates.

The FastAPI layer exposes health/readiness, chat, approvals, session reset, tool
discovery, and task inspection/cancellation. The CLI offers chat, voice, server,
health, doctor, tools, and version modes.
