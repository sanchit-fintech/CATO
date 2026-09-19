"""Bounded iterative agent runtime."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.agent_protocol import AgentAction, Observation, ProtocolError
from core.llm.base import ProviderError
from core.session import Session
from core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    response: str
    status: str
    session_id: str
    iterations: int


class AgentRuntime:
    def __init__(
        self, provider: Any, tools: ToolRegistry, *, max_iterations: int = 8
    ) -> None:
        self.provider, self.tools = provider, tools
        self.max_iterations = max_iterations

    def run(self, request: str, session: Session) -> RunResult:
        session.status = "running"
        session.iteration_count = 0
        session.add("user", request)
        context = [{"role": m.role, "content": m.content} for m in session.messages]
        last_error = "I couldn't complete that request."
        for iteration in range(1, self.max_iterations + 1):
            session.iteration_count = iteration
            try:
                raw = self._next_action(request, context)
                action = AgentAction.parse(raw)
            except ProtocolError:
                last_error = "I couldn't understand the model's next action."
                context.append(
                    {
                        "role": "system",
                        "content": (
                            "Previous action was malformed; return a valid action."
                        ),
                    }
                )
                continue
            except ProviderError:
                logger.error("provider_action_failed")
                session.status = "failed"
                return RunResult(
                    "I couldn't reach the language model. Please try again.",
                    "failed",
                    session.id,
                    iteration,
                )
            except Exception:
                logger.exception("provider_action_unexpected")
                session.status = "failed"
                return RunResult(
                    "I couldn't understand that request.",
                    "failed",
                    session.id,
                    iteration,
                )
            if action.type == "final":
                response = (action.response or "").strip()
                if not response:
                    response = "I understand, but I don't have an action for that yet."
                session.status = "completed"
                session.add("assistant", response)
                return RunResult(response, "completed", session.id, iteration)
            session.plan = action.plan or session.plan
            assert action.tool is not None
            result = self.tools.run(action.tool, **action.arguments)
            summary = (
                result.summary
                or result.error
                or (
                    "Action completed."
                    if result.ok
                    else "The action could not be completed."
                )
            )
            observation = Observation(
                action.tool,
                result.ok,
                summary,
                result.data if result.ok else None,
                result.code,
                result.requires_approval,
                result.truncated,
                result.metadata or {},
            )
            item = {"role": "observation", "content": observation.to_dict()}
            context.append(item)
            # Refusals never include sensitive data.
            persisted = observation.to_dict()
            persisted["data"] = None
            session.add("observation", persisted)
            last_error = summary
        session.status = "max_iterations"
        response = (
            f"I stopped after {self.max_iterations} steps to avoid an infinite loop. "
            f"Last result: {last_error}"
        )
        session.add("assistant", response)
        return RunResult(response, "max_iterations", session.id, self.max_iterations)

    def _next_action(self, request: str, context: list[dict[str, Any]]) -> object:
        method = getattr(self.provider, "next_action", None)
        if method is not None:
            return method(request, context, self.tools.schemas())
        # Compatibility for Mission 2 providers.
        if not any(item["role"] == "observation" for item in context):
            return self.provider.understand(request)
        observation = context[-1]["content"]
        if not observation["ok"]:
            return {"type": "final", "response": observation["summary"]}
        return {
            "type": "final",
            "response": self.provider.respond(request, observation),
        }
