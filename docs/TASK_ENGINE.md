# Durable task engine

```text
CLI / API -> TaskService -> SQLite queue -> Worker -> AgentRuntime -> Tools
                    |                         |
                    +---- task events <-------+
```

`POST /tasks` persists a queued task and returns immediately. The supervisor
atomically changes one queued task to running, attaches its worker ID, and starts
a fresh process using Python's `spawn` context. Each process reopens its own
database connections and uses the same runtime and approval policies as chat.

Steps and safe events are persisted after each action. Events contain tool names,
status, and error codes—not raw tool arguments or output. Task and event list
queries are bounded. On process startup, tasks left running or cancelling become
`interrupted`; Cato never invents a successful result after a crash. A user may
explicitly retry an interrupted or failed task.

Cancellation is immediately durable. Queued work cannot subsequently be claimed.
Running work is terminated as an owned process group, including descendants. The
supervisor also enforces a wall-clock limit independently of the agent loop.
