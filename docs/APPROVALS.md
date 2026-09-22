# Durable approvals

Non-secret approval actions are stored in a separate SQLite database. Records are
bound to task, session, tool, risk, expiration, and a SHA-256 fingerprint of the
canonical action arguments. Approval is one-use and transitions atomically from
pending to used, denied, or expired. Arguments and request continuation are
redacted after resolution.

Arguments that look like credentials or private keys are never made durable;
those approvals remain process-local. A task waiting for a durable approval may
survive restart and resume from safe structured observations. Hidden model
reasoning is never serialized.
