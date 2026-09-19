"""Validated, provider-independent agent actions and observations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


class ProtocolError(ValueError):
    """The provider returned an invalid agent action."""


@dataclass(frozen=True)
class AgentAction:
    type: Literal["tool", "final"]
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    response: str | None = None
    plan: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: object) -> AgentAction:
        if not isinstance(raw, dict):
            raise ProtocolError("Action must be an object.")
        action_type = raw.get("type")
        # Mission 2 compatibility.
        if action_type is None and "tool" in raw:
            action_type = "final" if raw.get("tool") in (None, "none") else "tool"
        if action_type == "tool":
            tool = raw.get("tool")
            arguments = raw.get("arguments", {})
            if not isinstance(tool, str) or not tool or not isinstance(arguments, dict):
                raise ProtocolError("Tool action is malformed.")
            plan = raw.get("plan", [])
            if not isinstance(plan, list) or not all(isinstance(x, str) for x in plan):
                raise ProtocolError("Plan is malformed.")
            return cls("tool", tool=tool, arguments=arguments, plan=plan)
        if action_type == "final":
            response = raw.get("response")
            if response is not None and not isinstance(response, str):
                raise ProtocolError("Final response is malformed.")
            return cls("final", response=response)
        raise ProtocolError("Unknown action type.")


@dataclass(frozen=True)
class Observation:
    tool: str
    ok: bool
    summary: str
    data: Any = None
    error_category: str | None = None
    requires_approval: bool = False
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
