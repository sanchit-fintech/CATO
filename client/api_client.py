"""Small standard-library client for Cato's local HTTP API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class APIError(RuntimeError):
    pass


class AgentClient(Protocol):
    def chat(self, message: str, session_id: str | None = None) -> dict[str, Any]: ...
    def approve(self, approval_id: str, session_id: str) -> dict[str, Any]: ...
    def deny(self, approval_id: str, session_id: str) -> dict[str, Any]: ...
    def health(self) -> dict[str, Any]: ...


@dataclass
class HTTPAgentClient:
    base_url: str = "http://127.0.0.1:8000"
    timeout_seconds: float = 30

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise APIError("Voice mode only connects to a local HTTP API.")

    def chat(self, message: str, session_id: str | None = None) -> dict[str, Any]:
        return self._request("/chat", {"message": message, "session_id": session_id})

    def approve(self, approval_id: str, session_id: str) -> dict[str, Any]:
        return self._request(
            f"/approvals/{approval_id}/approve", {"session_id": session_id}
        )

    def deny(self, approval_id: str, session_id: str) -> dict[str, Any]:
        return self._request(
            f"/approvals/{approval_id}/deny", {"session_id": session_id}
        )

    def health(self) -> dict[str, Any]:
        return self._request("/health", None, method="GET")

    def _request(
        self, path: str, payload: dict[str, Any] | None, *, method: str = "POST"
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise APIError("Cato's local API is unavailable.") from error
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise APIError("Cato's API returned an invalid response.") from error
        if not isinstance(result, dict):
            raise APIError("Cato's API returned an invalid response.")
        return result
