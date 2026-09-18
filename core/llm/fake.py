"""Deterministic model provider for tests and local development."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class FakeModelProvider:
    def __init__(
        self,
        decisions: Iterable[dict[str, Any]] | None = None,
        responses: Iterable[str] | None = None,
    ) -> None:
        self._decisions = iter(decisions or [])
        self._responses = iter(responses or [])

    def understand(self, command: str) -> dict[str, Any]:
        return next(
            self._decisions,
            {"intent": "unknown", "tool": None, "arguments": {}},
        )

    def respond(self, command: str, tool_result: dict[str, Any]) -> str:
        return next(self._responses, "Done.")
