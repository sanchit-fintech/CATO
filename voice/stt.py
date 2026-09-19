"""Speech-to-text provider contracts and implementations."""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from voice.audio import AudioInput


@dataclass(frozen=True)
class TranscriptionResult:
    success: bool
    text: str = ""
    error: str | None = None
    code: str | None = None
    duration_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SpeechToText(Protocol):
    def transcribe(self, audio: AudioInput) -> TranscriptionResult: ...
    def health(self) -> dict[str, str]: ...


class FakeSTT:
    def __init__(
        self, results: Iterable[str | TranscriptionResult | Exception]
    ) -> None:
        self._results = iter(results)

    def transcribe(self, audio: AudioInput) -> TranscriptionResult:
        try:
            result = next(self._results)
        except StopIteration:
            return TranscriptionResult(
                False, error="No fake transcription configured.", code="stt_empty"
            )
        if isinstance(result, Exception):
            raise result
        if isinstance(result, TranscriptionResult):
            return result
        text = result.strip()
        if not text:
            return TranscriptionResult(
                False, error="No speech was detected.", code="no_speech"
            )
        return TranscriptionResult(
            True, text=text, duration_seconds=audio.duration_seconds
        )

    def health(self) -> dict[str, str]:
        return {"status": "available", "provider": "fake"}


class FasterWhisperSTT:
    """Optional local Whisper provider; temporary WAV data is always removed."""

    def __init__(self, *, model: str = "base", device: str = "auto") -> None:
        self.model_name = model
        self.device = device
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise RuntimeError(
                    "Install Cato's voice dependencies for local STT."
                ) from error
            self._model = WhisperModel(self.model_name, device=self.device)
        return self._model

    def transcribe(self, audio: AudioInput) -> TranscriptionResult:
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                handle.write(audio.data)
                path = Path(handle.name)
            segments, info = self._load().transcribe(str(path), vad_filter=True)
            text = " ".join(segment.text.strip() for segment in segments).strip()
            if not text:
                return TranscriptionResult(
                    False, error="No speech was detected.", code="no_speech"
                )
            return TranscriptionResult(
                True,
                text=text,
                duration_seconds=audio.duration_seconds,
                metadata={
                    "language": getattr(info, "language", None),
                    "provider": "faster-whisper",
                },
            )
        except RuntimeError as error:
            return TranscriptionResult(False, error=str(error), code="stt_unavailable")
        except Exception:
            return TranscriptionResult(
                False, error="Speech transcription failed.", code="stt_failed"
            )
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def health(self) -> dict[str, str]:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return {
                "status": "missing_optional_dependency",
                "provider": "faster-whisper",
            }
        return {"status": "available", "provider": "faster-whisper"}
