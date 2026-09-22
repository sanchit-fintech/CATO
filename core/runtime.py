"""Bounded iterative agent runtime."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.agent_protocol import AgentAction, Observation, ProtocolError
from core.approvals import ApprovalStore
from core.llm.base import ProviderError
from core.session import Session
from core.tasks import AgentTask, TaskStatus, TaskStore
from core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    response: str
    status: str
    session_id: str
    iterations: int
    approval_id: str | None = None
    summary: str | None = None
    risk: str | None = None
    expires_at: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    duration_ms: float | None = None


class AgentRuntime:
    def __init__(
        self,
        provider: Any,
        tools: ToolRegistry,
        *,
        max_iterations: int = 8,
        approvals: ApprovalStore | None = None,
        tasks: TaskStore | None = None,
        max_runtime_seconds: float = 120,
        repeated_action_limit: int = 3,
    ) -> None:
        self.provider, self.tools = provider, tools
        self.max_iterations = max_iterations
        self.approvals = approvals or ApprovalStore()
        self.tasks = tasks or TaskStore()
        self.max_runtime_seconds = max(1, max_runtime_seconds)
        self.repeated_action_limit = max(2, repeated_action_limit)

    def run(
        self, request: str, session: Session, *, task: AgentTask | None = None
    ) -> RunResult:
        session.status = "running"
        session.iteration_count = 0
        session.add("user", request)
        task = task or self.tasks.create(session.id, request)
        task.status = TaskStatus.RUNNING
        task.started_at = task.started_at or datetime.now(UTC)
        task.add_step(kind="request", status="completed", summary="Request accepted.")
        self.tasks.save(task)
        self.tasks.append_event(task.id, "request_accepted", {})
        context = [{"role": m.role, "content": m.content} for m in session.messages]
        return self._loop(request, session, context, task)

    def resume(
        self, request: str, session: Session, observation: Observation
    ) -> RunResult:
        session.status = "running"
        context = [{"role": m.role, "content": m.content} for m in session.messages]
        context.append({"role": "observation", "content": observation.to_dict()})
        self._persist_observation(session, observation)
        waiting = next(
            (
                task
                for task in self.tasks.list(session_id=session.id)
                if task.status == TaskStatus.WAITING_FOR_APPROVAL
            ),
            None,
        )
        task = waiting or self.tasks.create(session.id, request)
        task.status = TaskStatus.RUNNING
        task.add_step(kind="approval", status="completed", summary=observation.summary)
        self.tasks.save(task)
        self.tasks.append_event(task.id, "approval_granted", {})
        return self._loop(request, session, context, task)

    def _loop(
        self,
        request: str,
        session: Session,
        context: list[dict[str, Any]],
        task: AgentTask,
    ) -> RunResult:
        last_error = "I couldn't complete that request."
        started = time.monotonic()
        fingerprints: dict[str, int] = {}
        for iteration in range(1, self.max_iterations + 1):
            persisted = self.tasks.get(task.id)
            if task.cancelled or (persisted is not None and persisted.cancelled):
                task.cancelled = True
                return self._finish(
                    task, session, "Task cancelled.", "cancelled", iteration, started
                )
            if time.monotonic() - started >= self.max_runtime_seconds:
                task.errors.append("max_runtime_exceeded")
                return self._finish(
                    task,
                    session,
                    "I stopped because the task runtime limit was reached.",
                    "timeout",
                    iteration,
                    started,
                )
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
                task.errors.append("provider_unavailable")
                return self._finish(
                    task,
                    session,
                    "I couldn't reach the language model. Please try again.",
                    "failed",
                    iteration,
                    started,
                )
            except Exception:
                logger.exception("provider_action_unexpected")
                session.status = "failed"
                task.errors.append("provider_error")
                return self._finish(
                    task,
                    session,
                    "I couldn't understand that request.",
                    "failed",
                    iteration,
                    started,
                )
            if action.type == "final":
                response = (action.response or "").strip()
                if not response:
                    response = "I understand, but I don't have an action for that yet."
                return self._finish(
                    task, session, response, "completed", iteration, started
                )
            session.plan = action.plan or session.plan
            assert action.tool is not None
            fingerprint = json.dumps(
                [action.tool, action.arguments], sort_keys=True, default=str
            )
            fingerprints[fingerprint] = fingerprints.get(fingerprint, 0) + 1
            if fingerprints[fingerprint] >= self.repeated_action_limit:
                task.errors.append("repeated_action")
                task.add_step(
                    kind="guard",
                    status="blocked",
                    summary="Repeated equivalent action detected.",
                    tool=action.tool,
                    error_code="repeated_action",
                )
                self.tasks.save(task)
                self.tasks.append_event(
                    task.id, "task_stalled", {"error_code": "repeated_action"}
                )
                return self._finish(
                    task,
                    session,
                    "I stopped because the same action was repeating without progress.",
                    "stalled",
                    iteration,
                    started,
                )
            definition = self.tools.get(action.tool)
            tool_started = time.monotonic()
            invalid = self.tools.validate(action.tool, action.arguments)
            if invalid is not None:
                result = invalid
            elif definition is not None and self.tools.needs_approval(
                action.tool, action.arguments
            ):
                approval = self.approvals.create(
                    task_id=task.id,
                    session_id=session.id,
                    tool=action.tool,
                    arguments=action.arguments,
                    request=request,
                    summary=self._approval_summary(action.tool, action.arguments),
                    risk=definition.risk,
                )
                session.status = "approval_required"
                task.status = TaskStatus.WAITING_FOR_APPROVAL
                task.add_step(
                    kind="approval",
                    status="waiting",
                    summary=approval.summary,
                    tool=action.tool,
                )
                self.tasks.save(task)
                self.tasks.append_event(
                    task.id, "approval_required", {"tool": action.tool}
                )
                return RunResult(
                    "Your approval is required before I perform this action.",
                    "approval_required",
                    session.id,
                    iteration,
                    approval.id,
                    approval.summary,
                    approval.risk,
                    approval.expires_at.isoformat(),
                    task.id,
                    task.request_id,
                    (time.monotonic() - started) * 1000,
                )
            else:
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
            self._persist_observation(session, observation)
            last_error = summary
            task.add_step(
                kind="tool",
                status="completed" if result.ok else "failed",
                summary=summary,
                tool=action.tool,
                error_code=result.code,
                duration_ms=(time.monotonic() - tool_started) * 1000,
            )
            self.tasks.save(task)
            self.tasks.append_event(
                task.id,
                "tool_completed" if result.ok else "tool_failed",
                {"tool": action.tool, "error_code": result.code},
            )
        response = (
            f"I stopped after {self.max_iterations} steps to avoid an infinite loop. "
            f"Last result: {last_error}"
        )
        task.errors.append("max_iterations")
        return self._finish(
            task,
            session,
            response,
            "max_iterations",
            self.max_iterations,
            started,
        )

    def _finish(
        self,
        task: AgentTask,
        session: Session,
        response: str,
        status: str,
        iterations: int,
        started: float,
    ) -> RunResult:
        session.status = status
        session.add("assistant", response)
        task.summary = response
        task.status = (
            TaskStatus.COMPLETED
            if status == "completed"
            else TaskStatus.CANCELLED
            if status == "cancelled"
            else TaskStatus.BLOCKED
            if status in {"stalled", "timeout", "max_iterations"}
            else TaskStatus.FAILED
        )
        task.updated_at = datetime.now(UTC)
        task.completed_at = task.updated_at
        self.tasks.save(task)
        self.tasks.append_event(
            task.id,
            "task_completed"
            if task.status == TaskStatus.COMPLETED
            else "task_cancelled"
            if task.status == TaskStatus.CANCELLED
            else "task_failed",
            {"status": status},
        )
        duration = (time.monotonic() - started) * 1000
        return RunResult(
            response,
            status,
            session.id,
            iterations,
            task_id=task.id,
            request_id=task.request_id,
            duration_ms=duration,
        )

    @staticmethod
    def _persist_observation(session: Session, observation: Observation) -> None:
        persisted = observation.to_dict()
        persisted["data"] = None
        session.add("observation", persisted)

    @staticmethod
    def _approval_summary(tool: str, arguments: dict[str, Any]) -> str:
        if tool == "mac_quit_app":
            return f"Quit {arguments.get('name', 'application')}"
        summaries = {
            "mac_clipboard_write": "Replace clipboard text",
            "file_move": "Move a file or folder",
            "file_write": "Overwrite an existing file",
        }
        return summaries.get(tool, f"Run {tool}")

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
