# Security model

- Configure narrow `CATO_APPROVED_ROOTS`; Cato is a policy boundary, not an OS sandbox.
- Writes never overwrite implicitly. Moves, app quits, clipboard replacement,
  and conditional overwrites use scoped, expiring, one-use approval tokens.
- Tool arguments are validated before approval creation and again before execution.
- Processes are launched without a shell and with bounded time and output.
- Sensitive filenames, binary/oversized reads, traversal, and symlink escapes are refused.
- Durable memory rejects likely secrets and private keys but is not encrypted at rest.
- Logs record action names and error classes, not prompts, arguments, or tool output.
- Destructive deletion, unrestricted terminal automation, screen capture, and
  background microphone monitoring are deliberately unavailable.

Report vulnerabilities privately to the repository owner. Do not include live
credentials, private user data, or exploit output in a public issue.
