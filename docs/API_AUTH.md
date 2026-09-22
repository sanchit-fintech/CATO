# Local API authentication

Set `CATO_API_TOKEN` to require `Authorization: Bearer TOKEN` on every endpoint.
Comparison is constant-time, and the token is never returned or logged. The CLI
client reads the same environment variable automatically.

Unauthenticated operation is permitted only on loopback hosts. Configuration is
rejected if `CATO_API_HOST` is non-loopback and no token is set. TLS and remote
pairing are outside the current local-only threat model.
