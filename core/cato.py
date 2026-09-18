"""Cato's small, synchronous CLI orchestration layer."""

from __future__ import annotations

import logging
from functools import partial

from core.config import ConfigurationError, Settings
from core.llm.base import ModelProvider, ProviderError
from core.llm.gemini import GeminiModelProvider
from core.tool_registry import ToolRegistry
from core.tool_types import ToolArgument, ToolDefinition
from tools.file_read import read_file
from tools.file_search import search_files

logger = logging.getLogger(__name__)


class Cato:
    def __init__(
        self,
        *,
        provider: ModelProvider | None = None,
        settings: Settings | None = None,
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
                    read_file, approved_roots=self.settings.approved_roots
                ),
                arguments={"path": ToolArgument(str)},
                read_only=True,
                risk="medium",
            )
        )

    def respond(self, command: str) -> str:
        command = command.strip()
        if not command:
            return "I didn't catch that."

        try:
            decision = self.provider.understand(command)
        except ProviderError:
            logger.error("provider_understand_failed")
            return "I couldn't reach the language model. Please try again."
        except Exception:
            logger.exception("provider_understand_unexpected")
            return "I couldn't understand that request."

        if not isinstance(decision, dict):
            return "I couldn't understand that request."
        tool_name = decision.get("tool")
        arguments = decision.get("arguments", {})
        if tool_name is None or tool_name == "none":
            return "I understand, but I don't have an action for that yet."
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return "I couldn't understand that request."

        logger.info("tool_selected", extra={"tool_name": tool_name})
        result = self.tools.run(tool_name, **arguments)
        if not result.ok:
            return result.error or "The action could not be completed."

        try:
            response = self.provider.respond(command, result.to_dict())
            if not isinstance(response, str) or not response.strip():
                return "The action completed, but I couldn't format the result."
            return response.strip()
        except ProviderError:
            logger.error("provider_respond_failed")
            return "The action completed, but I couldn't summarize the result."
        except Exception:
            logger.exception("provider_respond_unexpected")
            return "The action completed, but I couldn't summarize the result."


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

    print("Cato v0.6")
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
