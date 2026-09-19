# Mission 4 Engineering Report

## Initial architecture and design

Mission 3 provided a bounded iterative runtime, typed results, validating tool
registry, approved-root filesystem policy, restricted commands, sessions,
in-memory storage, Gemini/fake providers, and FastAPI. Mission 4 uses the existing
interception point between model action and tool execution.

Native operations live in `cato_platform/macos`. `MacOSController` receives
approved roots, an app allowlist, limits, and an injectable process runner.
Production uses argument arrays and `shell=False`; tests use a recorder. Imports
work on every OS, while native tools register only on macOS or when a test
controller is explicitly injected.

## Approval engine

`ApprovalStore` creates unpredictable UUID records tied to one session, tool,
exact arguments, and original request. Records expire, are single-use, and retain
terminal states to prevent replay. Cross-session authorization is rejected. The
runtime validates arguments first and pauses before execution. Approval consumes
the token, executes the exact stored action, sends its observation back to the
agent, and continues the original task. Stored arguments/request text are
scrubbed after approval, denial, or expiry.

## Tools and risk classifications

| Tool | Capability | Risk | Approval |
| --- | --- | --- | --- |
| `mac_open_app`, `mac_activate_app` | system | low | no |
| `mac_open_file`, `mac_open_folder`, `mac_reveal_in_finder` | system | low | no |
| `mac_open_url` | network | low | no |
| `mac_clipboard_read` | sensitive | low | no |
| `mac_clipboard_write` | write | moderate | yes |
| `mac_show_notification` | system | low | no |
| `mac_list_running_apps`, `mac_permission_status` | read-only | low | no |
| `mac_quit_app` | system | moderate | yes |
| `mac_open_in_vscode`, `mac_open_in_terminal` | system | low | no |
| existing `file_move` | write | medium | yes |
| existing `file_write` with overwrite | write | medium | conditional |

## API

`POST /chat` may return `approval_required` with an ID, safe summary, risk, and
expiry. `POST /approvals/{id}/approve` and `/deny` require the owning session ID.
Unknown, expired, denied, used, and cross-session tokens return structured
statuses without exposing arguments or stack traces.

## Security controls

- App names are syntax-checked and allowlisted; aliases normalize canonically.
- Path actions reuse canonical root/symlink checks and reject sensitive paths.
- URLs permit HTTP(S) only, require a host, and reject embedded credentials.
- Clipboard is UTF-8 text-only, bounded, unlogged, and not persisted in history.
- AppleScript source is fixed; all user values are separate `argv` entries.
- Quit is graceful, approval-gated, allowlisted, and protects critical apps.
- Terminal only opens an approved folder and never receives typed commands.
- There is no shell, `os.system`, force kill, persistence, or permission bypass.

## Tests and verification

Mission 4 tests cover app validation and aliases, activation, paths and symlinks,
Finder reveal, URL schemes, clipboard limits/logging, notification injection,
running apps, VS Code/Terminal, platform gating, permissions, approval ownership,
expiry, denial, replay, argument validation, continuation, and API routes. Fake
runners ensure tests never modify the desktop.

Manual verification was deliberately non-mutating: platform/tool registration
inspection plus fake-runner integration. No real app was opened or quit, no real
notification was sent, and the developer clipboard was not changed.

## Limitations and recommended Mission 5

Permission status remains conservative where macOS has no reliable public query.
Memory and approvals are process-local and restart clears them. App discovery is
not automatic; users configure an allowlist. GUI automation and destructive
control remain unavailable.

Mission 5 should add encrypted/redacted durable memory, authenticated multi-client
API ownership, a native approval UI, audit events, and narrowly scoped,
permission-aware UI capabilities without arbitrary input or screen control.
