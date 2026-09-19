from __future__ import annotations

import logging
from typing import Any

from core.tool_types import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "arguments": {
                    name: {"type": spec.type.__name__, "required": spec.required}
                    for name, spec in tool.arguments.items()
                },
                "risk": tool.risk,
                "capability": tool.capability,
            }
            for tool in self._tools.values()
        ]

    def run(self, name: str, **kwargs: Any) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.failure(
                "That action is not available.", code="unknown_tool"
            )

        unknown = set(kwargs) - set(tool.arguments)
        missing = {
            key
            for key, specification in tool.arguments.items()
            if specification.required and key not in kwargs
        }
        invalid = {
            key
            for key, value in kwargs.items()
            if key in tool.arguments
            and value is not None
            and (
                not isinstance(value, tool.arguments[key].type)
                or (tool.arguments[key].type is int and isinstance(value, bool))
            )
        }
        if unknown or missing or invalid:
            return ToolResult.failure(
                "The action arguments were invalid.", code="invalid_arguments"
            )

        logger.info("tool_execute", extra={"tool_name": name})
        try:
            result = tool.function(**kwargs)
            return (
                result if isinstance(result, ToolResult) else ToolResult.success(result)
            )
        except Exception as error:
            logger.error(
                "tool_failed",
                extra={"tool_name": name, "error_type": type(error).__name__},
            )
            return ToolResult.failure("The action could not be completed.")
