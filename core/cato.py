"""Cato's small, synchronous CLI orchestration layer."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
from dataclasses import asdict
from functools import partial

from cato_platform.macos import MacOSController
from core.agent_protocol import Observation
from core.approvals import ApprovalStore
from core.config import ConfigurationError, Settings
from core.llm.base import ModelProvider
from core.llm.gemini import GeminiModelProvider
from core.runtime import AgentRuntime, RunResult
from core.session import Session
from core.tasks import AgentTask, TaskStatus, TaskStore
from core.tool_registry import ToolRegistry
from core.tool_types import ToolArgument, ToolDefinition, ToolResult
from memory.persistent import MemoryRefused, SQLiteMemory
from memory.store import InMemoryStore, MemoryStore
from tools.command import run_command
from tools.developer import run_project_lint, run_project_tests
from tools.file_read import read_file
from tools.file_search import search_files
from tools.filesystem import (
    copy_path,
    create_directory,
    list_files,
    move_path,
    stat_file,
    write_file,
)
from tools.patch import apply_text_patch
from tools.project import discover_projects, git_inspect, inspect_project
from tools.transaction import apply_patch_set, rollback_patch_set

logger = logging.getLogger(__name__)
VERSION = "0.14.0"


class Cato:
    def __init__(
        self,
        *,
        provider: ModelProvider | None = None,
        settings: Settings | None = None,
        memory: MemoryStore | None = None,
        macos: MacOSController | None = None,
        platform_system: str | None = None,
        recover_tasks: bool = True,
    ) -> None:
        self.name = "Cato"
        self.settings = settings or Settings.load(require_api_key=provider is None)
        if provider is None:
            assert self.settings.gemini_api_key is not None
            provider = GeminiModelProvider(
                self.settings.gemini_api_key, self.settings.gemini_model
            )
        self.provider = provider
        self.approvals = ApprovalStore(
            ttl_seconds=self.settings.approval_ttl_seconds,
            path=self.settings.approval_database_path,
        )
        self.tasks = TaskStore(
            path=self.settings.task_database_path, recover_on_start=recover_tasks
        )
        self.platform_system = platform_system or platform.system()
        self.macos = macos
        if self.macos is None and self.platform_system == "Darwin":
            self.macos = MacOSController(
                self.settings.approved_roots,
                frozenset(self.settings.allowed_macos_apps),
                self.settings.max_clipboard_bytes,
            )
        self.tools = ToolRegistry()
        self.long_term_memory = (
            SQLiteMemory(self.settings.memory_path)
            if self.settings.memory_path is not None
            else None
        )
        self._register_tools()
        self.memory = memory or InMemoryStore(history_limit=self.settings.history_limit)
        self.runtime = AgentRuntime(
            self.provider,
            self.tools,
            max_iterations=self.settings.max_agent_iterations,
            approvals=self.approvals,
            tasks=self.tasks,
            max_runtime_seconds=self.settings.max_agent_runtime_seconds,
            repeated_action_limit=self.settings.repeated_action_limit,
        )

    def _register_tools(self) -> None:
        self.tools.register(
            ToolDefinition(
                name="file_search",
                description="Search filenames within approved filesystem roots.",
                function=partial(
                    search_files, approved_roots=self.settings.approved_roots
                ),
                arguments={
                    "query": ToolArgument(str),
                    "root": ToolArgument(str, required=False),
                    "extension": ToolArgument(str, required=False),
                    "modified_after": ToolArgument(str, required=False),
                },
                read_only=True,
                risk="low",
            )
        )
        self.tools.register(
            ToolDefinition(
                name="file_read",
                description="Read a non-sensitive text file in an approved root.",
                function=partial(
                    read_file,
                    approved_roots=self.settings.approved_roots,
                    max_bytes=self.settings.max_file_read_bytes,
                ),
                arguments={"path": ToolArgument(str)},
                read_only=True,
                risk="medium",
            )
        )
        registrations = [
            ToolDefinition(
                "project_discover",
                "Discover software projects below an approved root.",
                partial(discover_projects, approved_roots=self.settings.approved_roots),
                {
                    "root": ToolArgument(str),
                    "query": ToolArgument(str, False),
                    "max_depth": ToolArgument(int, False),
                    "max_results": ToolArgument(int, False),
                },
            ),
            ToolDefinition(
                "project_inspect",
                "Inspect project type, structure, test command, and Git state.",
                partial(inspect_project, approved_roots=self.settings.approved_roots),
                {"path": ToolArgument(str)},
            ),
            ToolDefinition(
                "git_inspect",
                "Read the branch, working tree state, and recent commits.",
                partial(git_inspect, approved_roots=self.settings.approved_roots),
                {"path": ToolArgument(str)},
            ),
            ToolDefinition(
                "project_run_tests",
                "Run configured Python tests with bounded output and runtime.",
                partial(
                    run_project_tests,
                    approved_roots=self.settings.approved_roots,
                    timeout=min(self.settings.max_agent_runtime_seconds, 600),
                    max_output_bytes=self.settings.max_command_output_bytes,
                ),
                {
                    "path": ToolArgument(str),
                    "target": ToolArgument(str, False),
                },
                False,
                "high",
                "system",
                True,
            ),
            ToolDefinition(
                "project_run_lint",
                "Run configured Ruff checks with bounded output and runtime.",
                partial(
                    run_project_lint,
                    approved_roots=self.settings.approved_roots,
                    timeout=min(self.settings.max_agent_runtime_seconds, 600),
                    max_output_bytes=self.settings.max_command_output_bytes,
                ),
                {"path": ToolArgument(str)},
                True,
                "medium",
                "system",
                True,
            ),
            ToolDefinition(
                "source_apply_patch",
                "Replace one exact source fragment after hash and worktree checks.",
                partial(
                    apply_text_patch,
                    approved_roots=self.settings.approved_roots,
                ),
                {
                    "path": ToolArgument(str),
                    "old_text": ToolArgument(str),
                    "new_text": ToolArgument(str),
                    "expected_sha256": ToolArgument(str, False),
                },
                False,
                "high",
                "write",
                True,
            ),
            ToolDefinition(
                "source_apply_patch_set",
                "Atomically apply a validated multi-file patch set with checkpoints.",
                partial(apply_patch_set, approved_roots=self.settings.approved_roots),
                {
                    "workspace": ToolArgument(str),
                    "task_id": ToolArgument(str),
                    "operations": ToolArgument(list),
                    "dry_run": ToolArgument(bool, False),
                },
                False,
                "high",
                "write",
                True,
            ),
            ToolDefinition(
                "source_rollback_patch_set",
                "Rollback only unchanged Cato-owned transactional edits.",
                partial(
                    rollback_patch_set, approved_roots=self.settings.approved_roots
                ),
                {"workspace": ToolArgument(str), "task_id": ToolArgument(str)},
                False,
                "high",
                "write",
                True,
            ),
            ToolDefinition(
                "filesystem_list",
                "List entries in an approved directory.",
                partial(list_files, approved_roots=self.settings.approved_roots),
                {
                    "path": ToolArgument(str),
                    "recursive": ToolArgument(bool, False),
                    "max_depth": ToolArgument(int, False),
                    "include_hidden": ToolArgument(bool, False),
                },
            ),
            ToolDefinition(
                "filesystem_stat",
                "Get safe filesystem metadata.",
                partial(stat_file, approved_roots=self.settings.approved_roots),
                {"path": ToolArgument(str)},
            ),
            ToolDefinition(
                "file_write",
                "Write a UTF-8 file without implicit overwrite.",
                partial(write_file, approved_roots=self.settings.approved_roots),
                {
                    "path": ToolArgument(str),
                    "content": ToolArgument(str),
                    "overwrite": ToolArgument(bool, False),
                },
                False,
                "medium",
                "write",
                approval_when=lambda arguments: bool(arguments.get("overwrite")),
            ),
            ToolDefinition(
                "file_create_directory",
                "Create a directory.",
                partial(create_directory, approved_roots=self.settings.approved_roots),
                {"path": ToolArgument(str), "parents": ToolArgument(bool, False)},
                False,
                "low",
                "write",
            ),
            ToolDefinition(
                "file_copy",
                "Copy a file or directory within approved roots.",
                partial(copy_path, approved_roots=self.settings.approved_roots),
                {
                    "source": ToolArgument(str),
                    "destination": ToolArgument(str),
                    "overwrite": ToolArgument(bool, False),
                },
                False,
                "medium",
                "write",
            ),
            ToolDefinition(
                "file_move",
                "Move a file or directory within approved roots.",
                partial(move_path, approved_roots=self.settings.approved_roots),
                {
                    "source": ToolArgument(str),
                    "destination": ToolArgument(str),
                    "overwrite": ToolArgument(bool, False),
                },
                False,
                "medium",
                "write",
                approval_required=True,
            ),
            ToolDefinition(
                "command_run",
                "Run a narrow allowlisted command without a shell.",
                partial(
                    run_command,
                    approved_roots=self.settings.approved_roots,
                    timeout=self.settings.command_timeout,
                    max_output_bytes=self.settings.max_command_output_bytes,
                ),
                {
                    "command": ToolArgument(str),
                    "arguments": ToolArgument(list),
                    "cwd": ToolArgument(str),
                },
                True,
                "medium",
                "system",
            ),
        ]
        for tool in registrations:
            self.tools.register(tool)
        if self.long_term_memory is not None:
            self.tools.register(
                ToolDefinition(
                    "memory_store",
                    "Store a durable non-secret user preference or project fact.",
                    self._memory_store,
                    {
                        "kind": ToolArgument(str),
                        "content": ToolArgument(str),
                        "importance": ToolArgument(int, False),
                    },
                    False,
                    "medium",
                    "write",
                )
            )
            self.tools.register(
                ToolDefinition(
                    "memory_search",
                    "Search durable non-secret memory.",
                    self._memory_search,
                    {
                        "query": ToolArgument(str),
                        "kind": ToolArgument(str, False),
                        "limit": ToolArgument(int, False),
                    },
                )
            )
        if self.macos is not None:
            self._register_macos_tools(self.macos)

    def _memory_store(self, kind: str, content: str, importance: int = 5) -> ToolResult:
        assert self.long_term_memory is not None
        try:
            record = self.long_term_memory.store(kind, content, importance=importance)
        except MemoryRefused as error:
            return ToolResult.failure(str(error), code="memory_refused")
        return ToolResult.success(asdict(record), summary="Memory stored.")

    def _memory_search(
        self, query: str, kind: str | None = None, limit: int = 10
    ) -> ToolResult:
        assert self.long_term_memory is not None
        records = self.long_term_memory.search(query, kind=kind, limit=limit)
        return ToolResult.success(
            [asdict(record) for record in records],
            summary=f"Found {len(records)} memories.",
        )

    def _register_macos_tools(self, macos: MacOSController) -> None:
        registrations = [
            ToolDefinition(
                "mac_open_app",
                "Open an allowed macOS application.",
                macos.open_app,
                {"name": ToolArgument(str)},
                True,
                "low",
                "system",
            ),
            ToolDefinition(
                "mac_activate_app",
                "Bring an allowed application to the foreground.",
                macos.activate_app,
                {"name": ToolArgument(str)},
                True,
                "low",
                "system",
            ),
            ToolDefinition(
                "mac_quit_app",
                "Gracefully quit an allowed non-protected application.",
                macos.quit_app,
                {"name": ToolArgument(str)},
                False,
                "moderate",
                "system",
                True,
            ),
            ToolDefinition(
                "mac_open_file",
                "Open an approved non-sensitive file.",
                partial(macos.open_path, directory=False),
                {"path": ToolArgument(str)},
                True,
                "low",
                "system",
            ),
            ToolDefinition(
                "mac_open_folder",
                "Open an approved folder.",
                partial(macos.open_path, directory=True),
                {"path": ToolArgument(str)},
                True,
                "low",
                "system",
            ),
            ToolDefinition(
                "mac_reveal_in_finder",
                "Reveal an approved path in Finder.",
                partial(macos.open_path, reveal=True),
                {"path": ToolArgument(str)},
                True,
                "low",
                "system",
            ),
            ToolDefinition(
                "mac_open_url",
                "Open an HTTP or HTTPS URL.",
                macos.open_url,
                {"url": ToolArgument(str)},
                True,
                "low",
                "network",
            ),
            ToolDefinition(
                "mac_clipboard_read",
                "Read bounded text from the clipboard.",
                macos.clipboard_read,
                {},
                True,
                "low",
                "sensitive",
            ),
            ToolDefinition(
                "mac_clipboard_write",
                "Replace clipboard text.",
                macos.clipboard_write,
                {"text": ToolArgument(str)},
                False,
                "moderate",
                "write",
                True,
            ),
            ToolDefinition(
                "mac_show_notification",
                "Show a bounded macOS notification.",
                macos.show_notification,
                {"title": ToolArgument(str), "message": ToolArgument(str)},
                True,
                "low",
                "system",
            ),
            ToolDefinition(
                "mac_list_running_apps",
                "List running foreground GUI applications.",
                macos.list_running_apps,
                {},
                True,
                "low",
                "read_only",
            ),
            ToolDefinition(
                "mac_open_in_vscode",
                "Open an approved path in Visual Studio Code.",
                macos.open_in_vscode,
                {"path": ToolArgument(str)},
                True,
                "low",
                "system",
            ),
            ToolDefinition(
                "mac_open_in_terminal",
                "Open an approved folder in Terminal without typing commands.",
                macos.open_in_terminal,
                {"path": ToolArgument(str)},
                True,
                "low",
                "system",
            ),
            ToolDefinition(
                "mac_permission_status",
                "Report conservative macOS permission availability.",
                macos.permission_status,
                {},
                True,
                "low",
                "read_only",
            ),
        ]
        for tool in registrations:
            self.tools.register(tool)

    def run(self, command: str, *, session_id: str | None = None) -> RunResult:
        command = command.strip()
        if not command:
            session = (
                self.memory.get(session_id) if session_id else self.memory.create()
            )
            assert session is not None
            return RunResult("I didn't catch that.", "invalid_request", session.id, 0)
        session = self.memory.get(session_id) if session_id else None
        session = session or self.memory.create()
        result = self.runtime.run(command, session)
        self.memory.save(session)
        return result

    def execute_task(self, task: AgentTask) -> RunResult:
        session = self.memory.get(task.session_id)
        if session is None:
            session = Session(
                id=task.session_id, history_limit=self.settings.history_limit
            )
        result = self.runtime.run(task.original_request, session, task=task)
        self.memory.save(session)
        return result

    def respond(self, command: str, *, session_id: str | None = None) -> str:
        return self.run(command, session_id=session_id).response

    def approve(self, approval_id: str, session_id: str) -> RunResult:
        session = self.memory.get(session_id)
        if session is None:
            session = Session(id=session_id, history_limit=self.settings.history_limit)
        decision = self.approvals.approve(approval_id, session_id)
        if not decision.ok or decision.approval is None:
            return RunResult(
                "Approval could not be used.", decision.code, session_id, 0
            )
        pending = decision.approval
        bound_task = self.tasks.get(pending.task_id) if pending.task_id else None
        if pending.task_id and (
            bound_task is None
            or bound_task.session_id != session_id
            or bound_task.status != TaskStatus.WAITING_FOR_APPROVAL
        ):
            pending.redact()
            return RunResult(
                "Approval is no longer bound to a waiting task.",
                "approval_task_mismatch",
                session_id,
                0,
            )
        arguments = dict(pending.arguments)
        request = pending.request
        result = self.tools.run(pending.tool, **arguments)
        pending.redact()
        observation = Observation(
            pending.tool,
            result.ok,
            result.summary or result.error or "Action completed.",
            result.data if result.ok else None,
            result.code,
            False,
            result.truncated,
            result.metadata or {},
        )
        completed = self.runtime.resume(request, session, observation)
        self.memory.save(session)
        return completed

    def deny(self, approval_id: str, session_id: str) -> RunResult:
        session = self.memory.get(session_id)
        if session is None:
            session = Session(id=session_id, history_limit=self.settings.history_limit)
        decision = self.approvals.deny(approval_id, session_id)
        if not decision.ok:
            return RunResult(
                "Approval could not be denied.", decision.code, session_id, 0
            )
        session.status = "denied"
        for task in self.tasks.list(session_id=session_id):
            if task.status == TaskStatus.WAITING_FOR_APPROVAL:
                task.status = TaskStatus.CANCELLED
                task.summary = "The pending action was denied."
                task.add_step(
                    kind="approval",
                    status="denied",
                    summary="Human approval was denied.",
                )
                self.tasks.save(task)
                self.tasks.append_event(task.id, "approval_denied", {})
                break
        session.add("assistant", "The pending action was denied and was not executed.")
        self.memory.save(session)
        return RunResult(
            "The pending action was denied and was not executed.",
            "denied",
            session_id,
            0,
        )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "chat"
    if mode in {"-h", "--help", "help"}:
        print(
            "Usage: cato [chat|voice|health|doctor|status|tools|server|run|tasks|task|"
            "--version]"
        )
        return
    if mode in {"-v", "--version", "version"}:
        print(f"Cato {VERSION}")
        return
    try:
        settings = Settings.load(require_api_key=mode in {"chat", ""})
        configure_logging(settings.log_level)
        logger.info("cato_starting")
    except ConfigurationError as error:
        print(f"Cato could not start: {error}")
        return

    if mode == "voice":
        if not settings.voice_enabled:
            print("Cato voice mode is disabled by configuration.")
            return
        from client.voice_client import run_voice_client

        run_voice_client(settings)
        return
    if mode == "health":
        try:
            from client.voice_client import build_voice_session

            health = build_voice_session(settings).health()
            print(f"Cato API: {health['api']['status']}")
            print(f"API URL: {health['api']['url']}")
            print(f"Microphone: {health['microphone']['status']}")
            print(f"Audio device: {health['microphone'].get('device', 'unknown')}")
            print(f"Audio permission: {health['microphone']['permission']}")
            print(
                f"STT: {health['stt']['status']} "
                f"({health['stt'].get('provider')}, "
                f"model={health['stt'].get('model', settings.stt_model)}, "
                "compute="
                f"{health['stt'].get('compute_type', settings.stt_compute_type)})"
            )
            print(f"TTS: {health['tts']['status']} ({health['tts']['provider']})")
            macos_status = (
                "available" if platform.system() == "Darwin" else "unavailable"
            )
            print(f"macOS support: {macos_status}")
            print(f"Voice ready: {'yes' if health['ready'] else 'no'}")
        except (ValueError, ConfigurationError) as error:
            print(f"Health check failed: {error}")
        return
    if mode == "doctor":
        memory_parent = settings.memory_path.parent if settings.memory_path else None
        existing_memory_parent = memory_parent
        while (
            existing_memory_parent is not None and not existing_memory_parent.exists()
        ):
            if existing_memory_parent.parent == existing_memory_parent:
                existing_memory_parent = None
                break
            existing_memory_parent = existing_memory_parent.parent
        checks = {
            "provider": bool(settings.gemini_api_key),
            "approved_roots": all(path.is_dir() for path in settings.approved_roots),
            "memory_parent": existing_memory_parent is not None
            and existing_memory_parent.is_dir()
            and os.access(existing_memory_parent, os.W_OK),
            "git": shutil.which("git") is not None,
            "macos": platform.system() == "Darwin",
            "task_database": settings.task_database_path is not None,
        }
        for name, ok in checks.items():
            print(f"{name}: {'ok' if ok else 'unavailable'}")
        print(f"overall: {'ready' if all(checks.values()) else 'attention needed'}")
        return
    if mode == "tools":
        from core.llm.fake import FakeModelProvider

        instance = Cato(provider=FakeModelProvider([]), settings=settings)
        for schema in instance.tools.schemas():
            print(f"{schema['name']}: {schema['description']}")
        return
    if mode == "server":
        import uvicorn

        uvicorn.run(
            "core.api:create_app",
            factory=True,
            host=settings.api_host,
            port=settings.api_port,
        )
        return
    if mode in {"run", "tasks", "task", "status"}:
        from client.api_client import APIError, HTTPAgentClient

        client = HTTPAgentClient(settings.voice_client_api_url)
        try:
            if mode == "status":
                result = client.status()
                print(f"Server: {result.get('server')}")
                print(f"Workers: {len(result.get('workers', []))}")
                print(f"Schedules: {result.get('schedules', 0)}")
                print(
                    f"Auth: {'enabled' if result.get('auth_enabled') else 'disabled'}"
                )
                for state, count in result.get("tasks", {}).items():
                    print(f"Tasks {state}: {count}")
            elif mode == "run":
                request = " ".join(sys.argv[2:]).strip()
                if not request:
                    print('Usage: cato run "task request"')
                    return
                submitted = client.submit_task(request)
                print(f"Task submitted: {submitted['task_id']}")
            elif mode == "tasks":
                status = sys.argv[2] if len(sys.argv) > 2 else None
                for task in client.tasks(status):
                    print(
                        f"{task['id']}  {task['status']}  {task.get('summary') or ''}"
                    )
            else:
                if len(sys.argv) < 3:
                    print("Usage: cato task <id> [events|cancel|retry]")
                    return
                task_id = sys.argv[2]
                action = sys.argv[3] if len(sys.argv) > 3 else "show"
                if action in {"--watch", "watch"}:
                    for event in client.stream_task_events(task_id):
                        print(f"{event.get('id', '-')}  {event.get('type', 'event')}")
                elif action == "events":
                    for event in client.task_events(task_id):
                        print(f"{event['id']}  {event['type']}")
                elif action in {"cancel", "retry"}:
                    result = client.task_action(task_id, action)
                    print(f"{result['task_id']}  {result['status']}")
                else:
                    task = client.task(task_id)
                    print(f"{task['id']}  {task['status']}")
                    print(task.get("summary") or "No result yet.")
        except APIError as error:
            print(f"Cato API error: {error}")
        return
    if mode not in {"chat", ""}:
        print(
            "Usage: cato [chat|voice|health|doctor|status|tools|server|run|tasks|task|"
            "--version]"
        )
        return

    cato = Cato(settings=settings)
    print(f"Cato v{VERSION}")
    print("Type 'exit' to shut down.\n")
    while True:
        try:
            command = input("You: ")
        except (EOFError, KeyboardInterrupt):
            print("\nCato: Shutting down.")
            break
        if command.lower() in {"exit", "quit"}:
            print("Cato: Shutting down.")
            break
        print(f"Cato: {cato.respond(command)}\n")


if __name__ == "__main__":
    main()
