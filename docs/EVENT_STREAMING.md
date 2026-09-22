# Event streaming

Polling remains available at `GET /tasks/{id}/events`. Live clients can use
`GET /tasks/{id}/events/stream`, which returns Server-Sent Events with monotonic
IDs. Reconnect with `Last-Event-ID`; Cato returns only later events. Streams emit
bounded database pages, periodic comment heartbeats, and close after terminal
state. Payloads contain safe operational metadata rather than prompts, arguments,
or tool output.
