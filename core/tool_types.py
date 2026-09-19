"""Shared contracts for safe tool registration and execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ToolArgument:
    type: type
    required: bool = True


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    function: Callable[..., Any]
    arguments: dict[str, ToolArgument]
    read_only: bool = True
    risk: str = "low"
    capability: Literal[
        "read_only", "write", "destructive", "system", "network", "sensitive"
    ] = "read_only"
    approval_required: bool = False
    approval_when: Callable[[dict[str, Any]], bool] | None = None


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    code: str | None = None
    requires_approval: bool = False
    summary: str | None = None
    truncated: bool = False
    metadata: dict[str, Any] | None = None

    @classmethod
    def success(
        cls,
        data: Any,
        *,
        summary: str | None = None,
        truncated: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        return cls(
            True, data=data, summary=summary, truncated=truncated, metadata=metadata
        )

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        code: str = "tool_error",
        requires_approval: bool = False,
    ) -> ToolResult:
        return cls(False, error=error, code=code, requires_approval=requires_approval)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
