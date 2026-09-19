# Cato

Cato is a local-first Python agent runtime for safely completing bounded,
multi-step tasks. It asks a model for one structured action at a time, validates
and executes registered tools, feeds structured observations back to the model,
and stops with either a final answer or a clear terminal status.

## Capabilities

- Iterative agent loop with typed tool/final actions, plans, recovery, and a
  configurable hard iteration limit.
- Session IDs, bounded conversation history, and a testable in-memory
  `MemoryStore` abstraction. Tool payloads are not persisted in session history.
- Approved-root filesystem search, listing, metadata, text reads, writes,
  directory creation, copy, and move.
- Traversal and symlink-escape prevention, sensitive-name filtering, binary and
  oversized-file refusal, bounded results, and explicit overwrite semantics.
- Structured command execution with no shell, approved working directories,
  timeout/output limits, and a narrow read-only command policy.
- Gemini provider plus a deterministic offline fake provider.
- CLI and FastAPI (`/health`, `/chat`, and session reset).
- Native macOS actions for allowed apps, approved paths, Finder, safe URLs,
  bounded text clipboard access, notifications, running-app discovery, VS Code,
  Terminal project opening, and conservative permission status.
- Expiring, session-bound, one-time approval tokens for moderate-risk actions.

Delete is intentionally unavailable until a human-confirmation flow exists.
Command execution is intentionally limited: `pwd`, `ls`, safe read-only Git
subcommands, and Python version inspection. Cato is a policy boundary, not an OS
sandbox; run it as a normal user with narrow approved roots.

macOS tools register only on macOS. App launching uses the configurable
`CATO_ALLOWED_MACOS_APPS` allowlist. Clipboard writes, app quits, file moves, and
file overwrites pause for explicit approval before execution. Cato does not grant
or bypass Accessibility, Automation, Notification, or Full Disk Access.

## Architecture

- `core/runtime.py`: iterative state machine and termination behavior
- `core/agent_protocol.py`: validated agent actions and observations
- `core/tool_registry.py`: argument validation and safe exception conversion
- `core/session.py`, `memory/store.py`: bounded session state and memory interface
- `tools/`: filesystem and command policies/implementations
- `core/llm/`: provider contract, Gemini adapter, and offline fake
- `core/api.py`: HTTP interface
- `core/approvals.py`: approval lifecycle and replay prevention
- `cato_platform/macos/`: injectable native macOS integration

Operational plans contain task steps only; Cato never asks for or exposes hidden
chain-of-thought. Tool failures are observations, allowing a later action to
recover. Logs contain event and tool names, not prompts, arguments, or outputs.

## Setup

Cato requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Set `GEMINI_API_KEY` in `.env`. Configure `CATO_APPROVED_ROOTS` as a
platform-path-separator-delimited list of narrow roots. The safe default is the
launch directory. Other settings and defaults are documented in `.env.example`.

## Run

```bash
python -m core.cato
```

To embed the API, use `core.api.create_app()`. For example, after installing an
ASGI server of your choice:

```bash
uvicorn 'core.api:create_app' --factory --host 127.0.0.1 --port 8000
```

Chat request:

```json
{"message": "List Python files in my project", "session_id": null}
```

The response contains `response`, `session_id`, `status`, and `iterations`.
Pass the returned session ID on later requests to continue that session.
Approval responses also contain `approval_id`, `summary`, `risk`, and
`expires_at`. Resolve one with the same session:

```text
POST /approvals/{approval_id}/approve  {"session_id":"..."}
POST /approvals/{approval_id}/deny     {"session_id":"..."}
```

An ID authorizes only its exact stored action. Arguments are not returned by the
API and are scrubbed after use, denial, or expiry.

## Verification

The suite is offline and covers multi-tool execution, recovery, termination,
filesystem boundaries, write safety, command policy, sessions, and the API.

```bash
pytest
ruff check .
```

## Next production steps

The memory interface is deliberately in-memory for now. A future encrypted or
carefully redacted SQLite implementation can implement `MemoryStore` without
changing runtime orchestration. High-risk/destructive capabilities should only
be added together with an explicit, auditable human-approval mechanism.

Native control intentionally excludes mouse/keyboard automation, screen capture,
unrestricted AppleScript, arbitrary Terminal commands, force-killing, system
setting changes, and background monitoring. Permission reporting is conservative
because macOS does not expose every grant through stable public APIs.
