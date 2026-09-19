"""Conversational voice state layered over the existing Cato API."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any

from client.api_client import AgentClient, APIError
from voice.audio import AudioCapture
from voice.stt import SpeechToText, TranscriptionResult
from voice.tts import TextToSpeech

YES = {"yes", "y", "approve", "approved", "confirm", "go ahead"}
NO = {"no", "n", "deny", "denied", "cancel", "do not", "don't"}


@dataclass(frozen=True)
class VoiceTurn:
    success: bool
    text: str
    status: str
    transcript: str | None = None
    tts_error: str | None = None


class VoiceSession:
    def __init__(
        self,
        *,
        client: AgentClient,
        capture: AudioCapture,
        stt: SpeechToText,
        tts: TextToSpeech,
        stt_timeout_seconds: float = 30,
        max_spoken_characters: int = 500,
    ) -> None:
        self.client = client
        self.capture = capture
        self.stt = stt
        self.tts = tts
        self.stt_timeout_seconds = max(0.001, stt_timeout_seconds)
        self.max_spoken_characters = max(80, max_spoken_characters)
        self.session_id: str | None = None
        self.pending_approval_id: str | None = None
        self.pending_summary: str | None = None

    def listen_once(self) -> VoiceTurn:
        captured = self.capture.capture()
        if not captured.success or captured.audio is None:
            return VoiceTurn(
                False,
                captured.error or "Recording failed.",
                captured.code or "audio_error",
            )
        transcription = self._transcribe(captured.audio)
        if not transcription.success:
            return VoiceTurn(
                False,
                transcription.error or "Transcription failed.",
                transcription.code or "stt_error",
            )
        return self.handle_text(transcription.text, transcript=transcription.text)

    def handle_text(self, text: str, *, transcript: str | None = None) -> VoiceTurn:
        clean = text.strip()
        if not clean:
            return VoiceTurn(False, "No speech was detected.", "no_speech", transcript)
        normalized = clean.casefold().rstrip(".!?")
        try:
            if normalized in YES | NO:
                if self.pending_approval_id is None or self.session_id is None:
                    return VoiceTurn(
                        False,
                        "There is no pending action to approve or deny.",
                        "no_pending_approval",
                        transcript,
                    )
                approval_id = self.pending_approval_id
                response = (
                    self.client.approve(approval_id, self.session_id)
                    if normalized in YES
                    else self.client.deny(approval_id, self.session_id)
                )
                self._clear_pending()
            elif self.pending_approval_id is not None:
                return VoiceTurn(
                    False,
                    "Please explicitly approve or deny the pending action first.",
                    "approval_pending",
                    transcript,
                )
            else:
                response = self.client.chat(clean, self.session_id)
                if (
                    isinstance(response, dict)
                    and response.get("status") == "invalid_session"
                ):
                    self.session_id = None
                    response = self.client.chat(clean, None)
        except APIError as error:
            return VoiceTurn(False, str(error), "api_unavailable", transcript)
        except Exception:
            return VoiceTurn(
                False,
                "The local Cato client failed safely.",
                "client_error",
                transcript,
            )
        return self._consume_response(response, transcript)

    def reset(self) -> None:
        self.session_id = None
        self._clear_pending()

    def cancel(self) -> None:
        self.capture.cancel()
        self.tts.cancel()

    def health(self) -> dict[str, Any]:
        try:
            api = self.client.health().get("status", "unknown")
        except Exception:
            api = "unavailable"
        microphone = self.capture.health()
        stt = self.stt.health()
        tts = self.tts.health()
        ready = (
            all(
                item == "available"
                for item in (
                    microphone.get("status"),
                    stt.get("status"),
                    tts.get("status"),
                )
            )
            and api == "ok"
        )
        return {
            "microphone": microphone,
            "stt": stt,
            "tts": tts,
            "api": api,
            "ready": ready,
        }

    def _transcribe(self, audio) -> TranscriptionResult:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.stt.transcribe, audio)
        try:
            return future.result(timeout=self.stt_timeout_seconds)
        except FutureTimeout:
            future.cancel()
            return TranscriptionResult(
                False, error="Speech transcription timed out.", code="stt_timeout"
            )
        except Exception:
            return TranscriptionResult(
                False, error="Speech transcription failed.", code="stt_failed"
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _consume_response(self, response: object, transcript: str | None) -> VoiceTurn:
        if not isinstance(response, dict):
            return VoiceTurn(
                False,
                "Cato returned a malformed response.",
                "malformed_response",
                transcript,
            )
        status = response.get("status")
        session_id = response.get("session_id")
        if not isinstance(status, str) or not isinstance(session_id, str):
            return VoiceTurn(
                False,
                "Cato returned a malformed response.",
                "malformed_response",
                transcript,
            )
        self.session_id = session_id
        if status == "approval_required":
            approval_id = response.get("approval_id")
            summary = response.get("summary")
            risk = response.get("risk")
            if not all(
                isinstance(item, str) and item for item in (approval_id, summary, risk)
            ):
                return VoiceTurn(
                    False,
                    "Cato returned a malformed approval.",
                    "malformed_response",
                    transcript,
                )
            self.pending_approval_id = approval_id
            self.pending_summary = summary
            text = f"{summary} requires {risk} risk approval. Approve this action?"
        else:
            text = response.get("response")
            if not isinstance(text, str) or not text.strip():
                return VoiceTurn(
                    False,
                    "Cato returned a malformed response.",
                    "malformed_response",
                    transcript,
                )
            if status.startswith("approval_") or status in {
                "denied",
                "invalid_session",
            }:
                self._clear_pending()
        spoken = self._spoken_version(text)
        tts_error = None
        try:
            speech = self.tts.speak(spoken)
            if not speech.success:
                tts_error = speech.error or "Text-to-speech failed."
        except Exception:
            tts_error = "Text-to-speech failed."
        return VoiceTurn(True, text, status, transcript, tts_error)

    def _spoken_version(self, text: str) -> str:
        if len(text) <= self.max_spoken_characters:
            return text
        prefix = text[: self.max_spoken_characters].rsplit(" ", 1)[0].rstrip()
        return f"{prefix}. The complete response is displayed in the terminal."

    def _clear_pending(self) -> None:
        self.pending_approval_id = None
        self.pending_summary = None
