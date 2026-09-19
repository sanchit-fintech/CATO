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

    def next_action(
        self, command: str, context: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            return next(self._decisions)
        except StopIteration:
            try:
                response = next(self._responses)
            except StopIteration:
                if context and context[-1].get("role") == "system":
                    return {
                        "type": "final",
                        "response": "I couldn't understand that request.",
                    }
                if (
                    context
                    and context[-1].get("role") == "observation"
                    and not context[-1]["content"].get("ok")
                ):
                    response = context[-1]["content"].get(
                        "summary", "The action could not be completed."
                    )
                else:
                    response = "Done."
            if not isinstance(response, str) or not response.strip():
                response = "The action completed, but I couldn't format the result."
            return {"type": "final", "response": response}

    def understand(self, command: str) -> dict[str, Any]:
        return next(
            self._decisions,
            {"intent": "unknown", "tool": None, "arguments": {}},
        )

    def respond(self, command: str, tool_result: dict[str, Any]) -> str:
        return next(self._responses, "Done.")
