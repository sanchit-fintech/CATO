from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from client.api_client import APIError, HTTPAgentClient
from client.service import LocalAPIManager
from core import cato as cato_module
from core.api import create_app
from core.config import Settings
from voice.stt import FasterWhisperSTT


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.base_url = "http://127.0.0.1:8000"

    def health(self):
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.return_code = None

    def poll(self):
        return self.return_code

    def terminate(self):
        self.terminated = True
        self.return_code = 0

    def kill(self):
        self.killed = True
        self.return_code = -9

    def wait(self, timeout=None):
        return self.return_code


def healthy():
    return {
        "status": "ok",
        "service": "cato",
        "ready": True,
        "provider_configured": True,
    }


def test_healthy_existing_cato_is_reused_without_starting() -> None:
    starts = []
    manager = LocalAPIManager(
        FakeClient([healthy()]), process_factory=lambda *a, **k: starts.append(a)
    )
    status = manager.ensure()
    assert status.ready and not status.owned and starts == []


def test_absent_api_is_started_and_owned(monkeypatch) -> None:
    process = FakeProcess()
    client = FakeClient([APIError("refused", code="connection_refused"), healthy()])
    manager = LocalAPIManager(
        client, process_factory=lambda *a, **k: process, startup_timeout=1
    )
    monkeypatch.setattr(manager, "_port_in_use", lambda: False)
    status = manager.ensure()
    assert status.ready and status.owned
    manager.stop()
    assert process.terminated and not process.killed


def test_non_cato_port_is_reported_and_unowned_process_is_never_stopped() -> None:
    client = FakeClient([{"status": "ok"}])
    manager = LocalAPIManager(client)
    status = manager.ensure(can_start=False)
    assert status.state == "incompatible_service"
    manager.stop()
    assert manager._process is None


def test_occupied_non_cato_port_uses_alternate_without_killing_owner(
    monkeypatch,
) -> None:
    process = FakeProcess()
    client = FakeClient(
        [
            {"status": "ok"},
            APIError("refused", code="connection_refused"),
            healthy(),
        ]
    )
    manager = LocalAPIManager(client, process_factory=lambda *a, **k: process)
    monkeypatch.setattr(
        manager,
        "_use_free_loopback_port",
        lambda: setattr(client, "base_url", "http://127.0.0.1:8123"),
    )
    monkeypatch.setattr(manager, "_port_in_use", lambda: False)
    assert manager.ensure().ready
    assert client.base_url.endswith(":8123")
    manager.stop()
    assert process.terminated


def test_malformed_health_is_distinguished() -> None:
    client = FakeClient([APIError("bad JSON", code="malformed_response")])
    status = LocalAPIManager(client).probe()
    assert status.state == "non_cato"
    assert "bad JSON" in status.message


def test_configuration_required_is_actionable() -> None:
    status = LocalAPIManager(
        FakeClient(
            [
                {
                    "status": "ok",
                    "service": "cato",
                    "ready": False,
                    "provider_configured": False,
                }
            ]
        )
    ).probe()
    assert status.state == "configuration_required"
    assert "GEMINI_API_KEY" in status.message


def test_http_client_distinguishes_refused_http_and_malformed(monkeypatch) -> None:
    client = HTTPAgentClient()

    def refused(*args, **kwargs):
        raise URLError(ConnectionRefusedError())

    monkeypatch.setattr("client.api_client.urlopen", refused)
    with pytest.raises(APIError, match="No service") as caught:
        client.health()
    assert caught.value.code == "connection_refused"

    def http_error(*args, **kwargs):
        body = json.dumps({"error": {"message": "Provider is not configured."}})
        raise HTTPError("url", 503, "error", {}, FakeBody(body.encode()))

    monkeypatch.setattr("client.api_client.urlopen", http_error)
    with pytest.raises(APIError, match="Provider is not configured") as caught:
        client.chat("hello")
    assert caught.value.code == "api_http_error" and caught.value.status == 503

    monkeypatch.setattr(
        "client.api_client.urlopen", lambda *a, **k: FakeResponse(b"not-json")
    )
    with pytest.raises(APIError, match="malformed JSON") as caught:
        client.health()
    assert caught.value.code == "malformed_response"


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self):
        return self.data

    def close(self):
        pass


class FakeResponse(FakeBody):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_whisper_model_is_initialized_once_with_supported_int8(monkeypatch) -> None:
    calls = []

    class WhisperModel:
        def __init__(self, model, **kwargs):
            calls.append((model, kwargs))

    monkeypatch.setitem(
        __import__("sys").modules,
        "faster_whisper",
        type("Module", (), {"WhisperModel": WhisperModel}),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "ctranslate2",
        type(
            "Module",
            (),
            {
                "get_supported_compute_types": staticmethod(
                    lambda device: {"int8", "float32"}
                )
            },
        ),
    )
    provider = FasterWhisperSTT(model="base", device="cpu", compute_type="auto")
    assert provider.prepare().success
    assert provider.prepare().success
    assert len(calls) == 1
    assert calls[0][1] == {"device": "cpu", "compute_type": "int8"}


def test_whisper_compute_falls_back_to_float32(monkeypatch) -> None:
    calls = []

    class WhisperModel:
        def __init__(self, model, **kwargs):
            calls.append(kwargs["compute_type"])
            if kwargs["compute_type"] == "int8":
                raise ValueError("unsupported")

    monkeypatch.setitem(
        __import__("sys").modules,
        "faster_whisper",
        type("Module", (), {"WhisperModel": WhisperModel}),
    )
    provider = FasterWhisperSTT(compute_type="auto")
    monkeypatch.setattr(provider, "_select_compute_type", lambda: "int8")
    assert provider.prepare().success
    assert calls == ["int8", "float32"]
    assert provider.selected_compute_type == "float32"


def test_realistic_voice_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "CATO_STT_TIMEOUT_SECONDS",
        "CATO_STT_DEVICE",
        "CATO_STT_COMPUTE_TYPE",
        "CATO_RECORDING_TIMEOUT_SECONDS",
        "CATO_SILENCE_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.load(require_api_key=False)
    assert settings.stt_timeout_seconds == 120
    assert settings.stt_device == "cpu"
    assert settings.stt_compute_type == "auto"
    assert settings.recording_timeout_seconds == 12
    assert settings.silence_threshold == 500


def test_health_command_reports_voice_capabilities(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    settings = Settings(None, "fake", (tmp_path,), "CRITICAL")

    class Session:
        def health(self):
            return {
                "api": {"status": "available", "url": "http://127.0.0.1:8000"},
                "microphone": {
                    "status": "available",
                    "permission": "unknown",
                    "device": "detected",
                },
                "stt": {
                    "status": "available",
                    "provider": "faster-whisper",
                    "model": "base",
                    "compute_type": "int8",
                },
                "tts": {"status": "available", "provider": "macos-say"},
                "ready": True,
            }

    monkeypatch.setattr(cato_module.Settings, "load", lambda **kwargs: settings)
    monkeypatch.setattr("client.voice_client.build_voice_session", lambda _: Session())
    monkeypatch.setattr(cato_module.sys, "argv", ["cato", "health"])
    cato_module.main()
    output = capsys.readouterr().out
    for text in (
        "Cato API: available",
        "Microphone: available",
        "Audio device: detected",
        "STT: available",
        "TTS: available",
        "Voice ready: yes",
    ):
        assert text in output


def test_unconfigured_api_reports_ready_false_and_chat_503(
    monkeypatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    client = TestClient(create_app())
    health = client.get("/health").json()
    assert health["service"] == "cato" and health["ready"] is False
    response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"
