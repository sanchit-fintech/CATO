from pathlib import Path

import pytest

from core.cato import Cato
from core.config import ConfigurationError, Settings
from core.llm.base import ProviderError
from core.llm.fake import FakeModelProvider


def settings(root: Path) -> Settings:
    return Settings(None, "fake", (root,), "CRITICAL")


def test_fake_provider_and_cato_orchestration(tmp_path: Path) -> None:
    file = tmp_path / "notes.txt"
    file.write_text("hello")
    provider = FakeModelProvider(
        decisions=[{"tool": "file_read", "arguments": {"path": str(file)}}],
        responses=["The note says hello."],
    )
    cato = Cato(provider=provider, settings=settings(tmp_path))
    assert cato.respond("read notes") == "The note says hello."


@pytest.mark.parametrize(
    "decision",
    [None, [], {"tool": [], "arguments": {}}, {"tool": "file_read", "arguments": []}],
)
def test_malformed_decisions_do_not_crash(tmp_path: Path, decision: object) -> None:
    provider = FakeModelProvider(decisions=[decision])  # type: ignore[list-item]
    assert "couldn't understand" in Cato(
        provider=provider, settings=settings(tmp_path)
    ).respond("hello")


def test_invalid_tool_and_arguments(tmp_path: Path) -> None:
    unknown = FakeModelProvider(decisions=[{"tool": "shell", "arguments": {}}])
    invalid = FakeModelProvider(decisions=[{"tool": "file_read", "arguments": {}}])
    assert "not available" in Cato(
        provider=unknown, settings=settings(tmp_path)
    ).respond("run")
    assert "arguments were invalid" in Cato(
        provider=invalid, settings=settings(tmp_path)
    ).respond("read")


class FailingProvider:
    def understand(self, command: str) -> dict:
        raise ProviderError("secret internal detail")

    def respond(self, command: str, tool_result: dict) -> str:
        raise AssertionError


def test_provider_failure_is_safe(tmp_path: Path) -> None:
    response = Cato(provider=FailingProvider(), settings=settings(tmp_path)).respond(
        "hello"
    )
    assert response == "I couldn't reach the language model. Please try again."
    assert "secret" not in response


def test_empty_provider_response_is_handled(tmp_path: Path) -> None:
    file = tmp_path / "note.txt"
    file.write_text("hello")
    provider = FakeModelProvider(
        decisions=[{"tool": "file_read", "arguments": {"path": str(file)}}],
        responses=[""],
    )
    response = Cato(provider=provider, settings=settings(tmp_path)).respond("read it")
    assert response == "The action completed, but I couldn't format the result."


def test_sensitive_result_never_reaches_provider(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("SECRET=value")

    class Provider(FakeModelProvider):
        def respond(self, command: str, tool_result: dict) -> str:
            raise AssertionError("sensitive result reached provider")

    provider = Provider(
        decisions=[{"tool": "file_read", "arguments": {"path": str(secret)}}]
    )
    response = Cato(provider=provider, settings=settings(tmp_path)).respond("read env")
    assert "requires explicit approval" in response


def test_missing_gemini_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        Settings.load()
