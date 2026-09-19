"""Explicit, bounded, in-memory microphone capture."""

from __future__ import annotations

import io
import threading
import time
import wave
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AudioInput:
    data: bytes
    sample_rate: int
    channels: int
    duration_seconds: float
    format: str = "wav"


@dataclass(frozen=True)
class CaptureResult:
    success: bool
    audio: AudioInput | None = None
    error: str | None = None
    code: str | None = None


class AudioCapture(Protocol):
    def capture(self) -> CaptureResult: ...
    def cancel(self) -> None: ...
    def health(self) -> dict[str, str]: ...


class SoundDeviceAudioCapture:
    """Captures one user-triggered WAV utterance without writing it to disk."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        silence_timeout_seconds: float = 1.5,
        sample_rate: int = 16_000,
        device: str | int | None = None,
        silence_threshold: float = 0.01,
    ) -> None:
        self.timeout_seconds = max(0.1, timeout_seconds)
        self.silence_timeout_seconds = max(0.1, silence_timeout_seconds)
        self.sample_rate = sample_rate
        self.device = device
        self.silence_threshold = silence_threshold
        self._cancel = threading.Event()
        self._active_stream = None

    def capture(self) -> CaptureResult:
        self._cancel.clear()
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            return CaptureResult(
                False,
                error="Microphone capture requires the optional voice dependencies.",
                code="audio_dependency_missing",
            )

        chunks: list[bytes] = []
        started = time.monotonic()
        last_sound = started
        heard_sound = False

        def callback(indata, frames, callback_time, status) -> None:
            del frames, callback_time, status
            nonlocal last_sound, heard_sound
            chunks.append(indata.copy().tobytes())
            if float(np.max(np.abs(indata))) >= self.silence_threshold:
                heard_sound = True
                last_sound = time.monotonic()

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=self.device,
                callback=callback,
            ) as stream:
                self._active_stream = stream
                while not self._cancel.is_set():
                    now = time.monotonic()
                    if now - started >= self.timeout_seconds:
                        break
                    if (
                        not heard_sound
                        and now - started >= self.silence_timeout_seconds
                    ):
                        break
                    if heard_sound and now - last_sound >= self.silence_timeout_seconds:
                        break
                    time.sleep(0.05)
        except Exception as error:
            code = self._audio_error_code(error)
            return CaptureResult(
                False, error=self._audio_error_message(code), code=code
            )
        finally:
            self._active_stream = None

        if self._cancel.is_set():
            return CaptureResult(False, error="Recording cancelled.", code="cancelled")
        if not heard_sound or not chunks:
            return CaptureResult(
                False, error="No speech was detected.", code="no_speech"
            )
        raw = b"".join(chunks)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(raw)
        duration = len(raw) / (self.sample_rate * 2)
        return CaptureResult(
            True, AudioInput(output.getvalue(), self.sample_rate, 1, duration)
        )

    def cancel(self) -> None:
        self._cancel.set()
        stream = self._active_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass

    def health(self) -> dict[str, str]:
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            available = any(
                int(item.get("max_input_channels", 0)) > 0 for item in devices
            )
            return {
                "status": "available" if available else "unavailable",
                "permission": "unknown",
            }
        except ImportError:
            return {"status": "missing_optional_dependency", "permission": "unknown"}
        except Exception:
            return {"status": "requires_user_action", "permission": "unknown"}

    @staticmethod
    def _audio_error_code(error: Exception) -> str:
        text = str(error).lower()
        if "permission" in text or "not permitted" in text:
            return "microphone_permission_denied"
        if "device" in text:
            return "audio_device_unavailable"
        return "audio_capture_failed"

    @staticmethod
    def _audio_error_message(code: str) -> str:
        if code == "microphone_permission_denied":
            return "Microphone permission is required in System Settings."
        if code == "audio_device_unavailable":
            return "No usable microphone was found."
        return "Microphone capture failed."
