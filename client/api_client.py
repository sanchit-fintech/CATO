"""Small standard-library client for Cato's local HTTP API."""

from __future__ import annotations

import json
import os
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
    api_token: str | None = None

    def __post_init__(self) -> None:
        self.api_token = self.api_token or os.getenv("CATO_API_TOKEN") or None
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

    def status(self) -> dict[str, Any]:
        return self._request("/status", None, method="GET")

    def submit_task(self, request: str, priority: int = 0) -> dict[str, Any]:
        return self._request("/tasks", {"request": request, "priority": priority})

    def tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        suffix = f"?status={status}" if status else ""
        return self._request(f"/tasks{suffix}", None, method="GET")

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request(f"/tasks/{task_id}", None, method="GET")

    def task_events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self._request(
            f"/tasks/{task_id}/events?after={after}", None, method="GET"
        )

    def task_action(self, task_id: str, action: str) -> dict[str, Any]:
        if action not in {"cancel", "retry"}:
            raise APIError("Unsupported task action.")
        return self._request(f"/tasks/{task_id}/{action}", {})

    def stream_task_events(self, task_id: str, after: int = 0):
        headers = {"Accept": "text/event-stream", "Last-Event-ID": str(after)}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/tasks/{task_id}/events/stream",
            method="GET",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=max(self.timeout_seconds, 600)) as response:
                event: dict[str, Any] = {}
                for raw in response:
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    if not line and event:
                        yield event
                        event = {}
                    elif line.startswith("id: "):
                        event["id"] = int(line[4:])
                    elif line.startswith("event: "):
                        event["type"] = line[7:]
                    elif line.startswith("data: "):
                        event["data"] = json.loads(line[6:])
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise APIError("Task event stream disconnected.") from error

    def _request(
        self, path: str, payload: dict[str, Any] | None, *, method: str = "POST"
    ) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=data,
            method=method,
            headers=headers,
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
        if not isinstance(result, (dict, list)):
            raise APIError(
                "The service returned an invalid response and may not be Cato.",
                code="malformed_response",
            )
        return result
