from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from client.api_client import APIError, HTTPAgentClient
from voice.audio import AudioInput, CaptureResult, SoundDeviceAudioCapture
from voice.session import VoiceSession
from voice.stt import FakeSTT, FasterWhisperSTT, TranscriptionResult
from voice.tts import FakeTTS, MacOSSayTTS, SpeechResult

AUDIO = AudioInput(b"RIFFfake", 16_000, 1, 0.5)


class FakeCapture:
    def __init__(self, results=None) -> None:
        self.results = iter(results or [CaptureResult(True, AUDIO)])
        self.cancelled = False

    def capture(self):
        return next(self.results)

    def cancel(self):
        self.cancelled = True

    def health(self):
        return {"status": "available", "permission": "unknown"}


class FakeAPI:
    def __init__(self, responses=None) -> None:
        self.responses = iter(responses or [])
        self.calls = []

    def chat(self, message, session_id=None):
        self.calls.append(("chat", message, session_id))
        return next(self.responses)

    def approve(self, approval_id, session_id):
        self.calls.append(("approve", approval_id, session_id))
        return next(self.responses)

    def deny(self, approval_id, session_id):
        self.calls.append(("deny", approval_id, session_id))
        return next(self.responses)

    def health(self):
        return {"status": "ok", "service": "cato", "ready": True}


def response(text="Done.", session="session-1", status="completed"):
    return {"response": text, "session_id": session, "status": status, "iterations": 1}


def test_fake_stt_success_empty_exception_and_determinism() -> None:
    stt = FakeSTT(["hello", "  ", RuntimeError("offline")])
    assert stt.transcribe(AUDIO).text == "hello"
    assert stt.transcribe(AUDIO).code == "no_speech"
    with pytest.raises(RuntimeError):
        stt.transcribe(AUDIO)
    assert stt.health()["provider"] == "fake"


def test_local_stt_temporary_audio_is_always_removed(
    tmp_path: Path, monkeypatch
) -> None:
    import tempfile

    real_temporary_file = tempfile.NamedTemporaryFile

    def temporary_file(**kwargs):
        return real_temporary_file(dir=tmp_path, **kwargs)

    class Segment:
        text = "hello locally"

    class Model:
        def transcribe(self, path, vad_filter=True):
            assert Path(path).exists() and vad_filter
            return [Segment()], SimpleNamespace(language="en")

    provider = FasterWhisperSTT()
    provider._model = Model()
    monkeypatch.setattr("voice.stt.tempfile.NamedTemporaryFile", temporary_file)
    result = provider.transcribe(AUDIO)
    assert result.success and result.text == "hello locally"
    assert list(tmp_path.iterdir()) == []


def test_voice_session_handles_stt_exception_and_timeout() -> None:
    class SlowSTT:
        def transcribe(self, audio):
            time.sleep(0.1)
            return TranscriptionResult(True, "late")

        def health(self):
            return {"status": "available"}

    timed = VoiceSession(
        client=FakeAPI(),
        capture=FakeCapture(),
        stt=SlowSTT(),
        tts=FakeTTS(),
        stt_timeout_seconds=0.01,
    )
    assert timed.listen_once().status == "stt_timeout"
    failed = VoiceSession(
        client=FakeAPI(),
        capture=FakeCapture(),
        stt=FakeSTT([RuntimeError("secret")]),
        tts=FakeTTS(),
    )
    turn = failed.listen_once()
    assert turn.status == "stt_failed" and "secret" not in turn.text


class FakeProcess:
    def __init__(self, returncode=0) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return None if not self.terminated else self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_macos_tts_uses_safe_arguments_for_special_text() -> None:
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()

    text = 'hello "quoted"; $(bad)\nnext'
    result = MacOSSayTTS(voice="Samantha", rate=180, popen=popen).speak(text)
    assert result.success
    assert calls[0][0] == ["/usr/bin/say", "-v", "Samantha", "-r", "180", "--", text]
    assert calls[0][1]["shell"] is False


def test_tts_cancellation_only_terminates_owned_process() -> None:
    process = FakeProcess()
    tts = MacOSSayTTS(popen=lambda *args, **kwargs: process)
    tts._process = process
    tts.cancel()
    assert process.terminated and not process.killed
    fake = FakeTTS()
    fake.cancel()
    assert fake.cancelled


def test_tts_failure_does_not_lose_text_response() -> None:
    api = FakeAPI([response("Visible answer")])
    tts = FakeTTS([SpeechResult(False, error="speaker unavailable", code="tts_failed")])
    turn = VoiceSession(
        client=api, capture=FakeCapture(), stt=FakeSTT([]), tts=tts
    ).handle_text("question")
    assert turn.text == "Visible answer" and turn.tts_error == "speaker unavailable"


def test_session_is_created_reused_and_reset() -> None:
    api = FakeAPI(
        [response("one", "abc"), response("two", "abc"), response("three", "new")]
    )
    session = VoiceSession(
        client=api, capture=FakeCapture(), stt=FakeSTT([]), tts=FakeTTS()
    )
    session.handle_text("first")
    session.handle_text("second")
    assert api.calls[:2] == [("chat", "first", None), ("chat", "second", "abc")]
    session.reset()
    session.handle_text("third")
    assert api.calls[2] == ("chat", "third", None)


def test_missing_server_session_recovers_with_new_chat() -> None:
    api = FakeAPI(
        [
            {"response": "missing", "session_id": "old", "status": "invalid_session"},
            response("Recovered", "new"),
        ]
    )
    session = VoiceSession(
        client=api, capture=FakeCapture(), stt=FakeSTT([]), tts=FakeTTS()
    )
    session.session_id = "old"
    turn = session.handle_text("continue")
    assert turn.text == "Recovered" and session.session_id == "new"
    assert api.calls[-1] == ("chat", "continue", None)


def test_approval_prompt_and_exact_typed_or_spoken_approval() -> None:
    pending = {
        "response": "approval",
        "session_id": "s1",
        "status": "approval_required",
        "approval_id": "a1",
        "summary": "Quit Notes",
        "risk": "moderate",
    }
    api = FakeAPI([pending, response("Notes was quit", "s1")])
    session = VoiceSession(
        client=api, capture=FakeCapture(), stt=FakeSTT([]), tts=FakeTTS()
    )
    prompt = session.handle_text("quit Notes")
    assert "Quit Notes" in prompt.text and session.pending_approval_id == "a1"
    completed = session.handle_text("Yes.")
    assert completed.text == "Notes was quit"
    assert api.calls[-1] == ("approve", "a1", "s1")
    assert session.pending_approval_id is None


def test_spoken_denial_and_no_pending_yes_do_not_chat() -> None:
    pending = {
        "response": "approval",
        "session_id": "s1",
        "status": "approval_required",
        "approval_id": "exact",
        "summary": "Quit Safari",
        "risk": "moderate",
    }
    api = FakeAPI([pending, response("Denied", "s1", "denied")])
    session = VoiceSession(
        client=api, capture=FakeCapture(), stt=FakeSTT([]), tts=FakeTTS()
    )
    assert session.handle_text("yes").status == "no_pending_approval"
    assert not api.calls
    session.handle_text("quit Safari")
    result = session.handle_text("no")
    assert result.status == "denied" and api.calls[-1] == ("deny", "exact", "s1")
    assert session.pending_approval_id is None


def test_pending_approval_blocks_unrelated_utterance() -> None:
    pending = {
        "response": "approval",
        "session_id": "s1",
        "status": "approval_required",
        "approval_id": "a",
        "summary": "Quit Notes",
        "risk": "moderate",
    }
    api = FakeAPI([pending])
    session = VoiceSession(
        client=api, capture=FakeCapture(), stt=FakeSTT([]), tts=FakeTTS()
    )
    session.handle_text("quit")
    turn = session.handle_text("open Safari")
    assert turn.status == "approval_pending" and len(api.calls) == 1


def test_expired_or_replayed_approval_clears_pending() -> None:
    pending = {
        "response": "approval",
        "session_id": "s",
        "status": "approval_required",
        "approval_id": "old",
        "summary": "Quit Notes",
        "risk": "moderate",
    }
    expired = {
        "response": "Approval could not be used.",
        "session_id": "s",
        "status": "approval_expired",
        "iterations": 0,
    }
    api = FakeAPI([pending, expired])
    session = VoiceSession(
        client=api, capture=FakeCapture(), stt=FakeSTT([]), tts=FakeTTS()
    )
    session.handle_text("quit")
    assert session.handle_text("approve").status == "approval_expired"
    assert session.pending_approval_id is None


def test_long_response_is_fully_displayed_but_bounded_for_speech() -> None:
    full = "word " * 200
    tts = FakeTTS()
    session = VoiceSession(
        client=FakeAPI([response(full)]),
        capture=FakeCapture(),
        stt=FakeSTT([]),
        tts=tts,
        max_spoken_characters=100,
    )
    turn = session.handle_text("long answer")
    assert turn.text == full
    assert len(tts.spoken[0]) < len(full)
    assert "complete response is displayed" in tts.spoken[0]


def test_api_unavailable_and_malformed_response_are_recoverable() -> None:
    class OfflineAPI(FakeAPI):
        def chat(self, message, session_id=None):
            raise APIError("API unavailable")

    offline = VoiceSession(
        client=OfflineAPI(), capture=FakeCapture(), stt=FakeSTT([]), tts=FakeTTS()
    )
    assert offline.handle_text("hi").status == "api_unavailable"
    malformed = VoiceSession(
        client=FakeAPI([["bad"]]), capture=FakeCapture(), stt=FakeSTT([]), tts=FakeTTS()
    )
    assert malformed.handle_text("hi").status == "malformed_response"


@pytest.mark.parametrize(
    "url", ["file:///tmp/cato", "http://example.com", "ftp://127.0.0.1"]
)
def test_http_voice_client_rejects_nonlocal_or_unsafe_api_urls(url: str) -> None:
    with pytest.raises(APIError):
        HTTPAgentClient(url)


def test_http_voice_client_accepts_loopback() -> None:
    assert HTTPAgentClient("http://127.0.0.1:8000").base_url.endswith("8000")
    assert HTTPAgentClient("http://localhost:8000").base_url.endswith("8000")


def test_voice_health_and_cancel() -> None:
    capture = FakeCapture()
    tts = FakeTTS()
    session = VoiceSession(client=FakeAPI(), capture=capture, stt=FakeSTT([]), tts=tts)
    assert session.health()["ready"]
    session.cancel()
    assert capture.cancelled and tts.cancelled


def test_audio_optional_dependency_absence(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    monkeypatch.setitem(sys.modules, "numpy", None)
    result = SoundDeviceAudioCapture().capture()
    assert result.code == "audio_dependency_missing"


def test_audio_permission_failure(monkeypatch) -> None:
    class InputStream:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("Microphone permission not permitted")

        def __exit__(self, *args):
            pass

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(max=max, abs=abs))
    monkeypatch.setitem(
        sys.modules, "sounddevice", SimpleNamespace(InputStream=InputStream)
    )
    result = SoundDeviceAudioCapture(timeout_seconds=0.1).capture()
    assert result.code == "microphone_permission_denied"


def test_audio_recording_start_stop_and_in_memory_wav(monkeypatch) -> None:
    class Chunk:
        def astype(self, dtype):
            return self

        def copy(self):
            return self

        def tobytes(self):
            return b"\x01\x00" * 160

    class InputStream:
        def __init__(self, callback, **kwargs):
            self.callback = callback

        def __enter__(self):
            self.callback(Chunk(), 160, None, None)
            return self

        def __exit__(self, *args):
            pass

        def abort(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "numpy",
        SimpleNamespace(max=lambda _: 600, abs=lambda value: value),
    )
    monkeypatch.setitem(
        sys.modules, "sounddevice", SimpleNamespace(InputStream=InputStream)
    )
    result = SoundDeviceAudioCapture(
        timeout_seconds=0.05, silence_timeout_seconds=0.01
    ).capture()
    assert result.success and result.audio.data.startswith(b"RIFF")


def test_audio_cancellation(monkeypatch) -> None:
    class InputStream:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def abort(self):
            pass

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(max=max, abs=abs))
    monkeypatch.setitem(
        sys.modules, "sounddevice", SimpleNamespace(InputStream=InputStream)
    )
    capture = SoundDeviceAudioCapture(timeout_seconds=1)
    result = []
    thread = threading.Thread(target=lambda: result.append(capture.capture()))
    thread.start()
    time.sleep(0.02)
    capture.cancel()
    thread.join(timeout=1)
    assert result[0].code == "cancelled"
