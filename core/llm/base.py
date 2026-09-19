"""Small provider contract used by the Cato runtime."""

from __future__ import annotations

from typing import Any, Protocol


class ProviderError(RuntimeError):
    """A safe wrapper for model-provider failures."""


class ModelProvider(Protocol):
    def next_action(
        self, command: str, context: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]: ...

    def understand(self, command: str) -> dict[str, Any]: ...

    def respond(self, command: str, tool_result: dict[str, Any]) -> str: ...
