"""Small standard-library client for Cato's local HTTP API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class APIError(RuntimeError):
    def __init__(
        self, message: str, *, code: str = "api_error", status: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


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
        except HTTPError as error:
            try:
                body = json.loads(error.read())
                message = body.get("error", {}).get("message")
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                message = None
            raise APIError(
                message or f"Cato API returned HTTP {error.code}.",
                code="api_http_error",
                status=error.code,
            ) from error
        except URLError as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, ConnectionRefusedError):
                message = "No service is listening at the configured Cato API URL."
                code = "connection_refused"
            else:
                message = "The configured local API could not be reached."
                code = "connection_failed"
            raise APIError(message, code=code) from error
        except TimeoutError as error:
            raise APIError(
                "The local API request timed out.", code="api_timeout"
            ) from error
        except OSError as error:
            raise APIError(
                "The local API connection failed.", code="connection_failed"
            ) from error
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise APIError(
                "The service returned malformed JSON and may not be Cato.",
                code="malformed_response",
            ) from error
        if not isinstance(result, dict):
            raise APIError(
                "The service returned an invalid response and may not be Cato.",
                code="malformed_response",
            )
        return result
