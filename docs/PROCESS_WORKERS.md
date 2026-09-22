# Process workers

The server owns a lightweight supervisor thread. It atomically claims one task,
then starts a fresh `spawn`-based Python process. The child reopens Cato's SQLite
databases and executes exactly one task. Mutable connections are never shared.

Each attempt records its worker ID, PID, random spawn token, start/finish time,
exit reason, exit code, and safe error code. The spawn token and live process
handle distinguish owned children from unrelated processes and protect against
PID reuse.

The child creates a new process group before running provider or tool code.
Cancellation and hard timeout signal only the live child handle owned by the
supervisor, first with `SIGTERM` and then `SIGKILL` after a grace period. This
also terminates descendants such as test processes. Cato never kills a process
based solely on a PID recovered from SQLite.

The child environment removes variables whose names indicate tokens, passwords,
API keys, private keys, or secrets. The Gemini provider is reconstructed from
explicit settings before sanitization; project subprocesses do not inherit the
provider credential.

SQLite-backed stores use WAL mode and a bounded five-second busy timeout so brief
parent/child write overlap waits instead of failing immediately. A server shutdown
marks active work as interrupted, rather than crashed, so it can be retried safely.
