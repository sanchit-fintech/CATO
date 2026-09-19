"""Cato's small, synchronous CLI orchestration layer."""

from __future__ import annotations

import logging
from functools import partial

from core.config import ConfigurationError, Settings
from core.llm.base import ModelProvider
from core.llm.gemini import GeminiModelProvider
from core.runtime import AgentRuntime, RunResult
from core.tool_registry import ToolRegistry
from core.tool_types import ToolArgument, ToolDefinition
from memory.store import InMemoryStore, MemoryStore
from tools.command import run_command
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

logger = logging.getLogger(__name__)


class Cato:
    def __init__(
        self,
        *,
        provider: ModelProvider | None = None,
        settings: Settings | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.name = "Cato"
        self.settings = settings or Settings.load(require_api_key=provider is None)
        if provider is None:
            assert self.settings.gemini_api_key is not None
            provider = GeminiModelProvider(
                self.settings.gemini_api_key, self.settings.gemini_model
            )
        self.provider = provider
        self.tools = ToolRegistry()
        self._register_tools()
        self.memory = memory or InMemoryStore(history_limit=self.settings.history_limit)
        self.runtime = AgentRuntime(
            self.provider, self.tools, max_iterations=self.settings.max_agent_iterations
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

    def respond(self, command: str, *, session_id: str | None = None) -> str:
        return self.run(command, session_id=session_id).response


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    try:
        settings = Settings.load()
        configure_logging(settings.log_level)
        logger.info("cato_starting")
        cato = Cato(settings=settings)
    except ConfigurationError as error:
        print(f"Cato could not start: {error}")
        return

    print("Cato v0.7")
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
