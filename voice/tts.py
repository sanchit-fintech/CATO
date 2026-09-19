"""Safe, cancellable text-to-speech providers."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SpeechResult:
    success: bool
    error: str | None = None
    code: str | None = None
    interrupted: bool = False


class TextToSpeech(Protocol):
    def speak(self, text: str) -> SpeechResult: ...
    def cancel(self) -> None: ...
    def health(self) -> dict[str, str]: ...


class MacOSSayTTS:
    def __init__(
        self,
        *,
        voice: str | None = None,
        rate: int | None = None,
        timeout_seconds: float = 120,
        popen=subprocess.Popen,
    ) -> None:
        self.voice = voice
        self.rate = rate
        self.timeout_seconds = timeout_seconds
        self._popen = popen
        self._process = None
        self._lock = threading.Lock()

    def speak(self, text: str) -> SpeechResult:
        if not text.strip():
            return SpeechResult(
                False, error="There is no text to speak.", code="tts_empty"
            )
        argv = ["/usr/bin/say"]
        if self.voice:
            argv.extend(["-v", self.voice])
        if self.rate is not None:
            argv.extend(["-r", str(self.rate)])
        argv.extend(["--", text])
        try:
            process = self._popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
            with self._lock:
                self._process = process
            return_code = process.wait(timeout=self.timeout_seconds)
            if return_code != 0:
                return SpeechResult(
                    False, error="Text-to-speech failed.", code="tts_failed"
                )
            return SpeechResult(True)
        except FileNotFoundError:
            return SpeechResult(
                False,
                error="macOS text-to-speech is unavailable.",
                code="tts_unavailable",
            )
        except subprocess.TimeoutExpired:
            self.cancel()
            return SpeechResult(
                False,
                error="Text-to-speech timed out.",
                code="tts_timeout",
                interrupted=True,
            )
        except KeyboardInterrupt:
            self.cancel()
            return SpeechResult(
                False,
                error="Text-to-speech was interrupted.",
                code="tts_interrupted",
                interrupted=True,
            )
        except OSError:
            return SpeechResult(
                False, error="Text-to-speech failed.", code="tts_failed"
            )
        finally:
            with self._lock:
                self._process = None

    def cancel(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def health(self) -> dict[str, str]:
        from pathlib import Path

        return {
            "status": "available" if Path("/usr/bin/say").exists() else "unavailable",
            "provider": "macos-say",
        }


class FakeTTS:
    def __init__(
        self, results: Iterable[SpeechResult | Exception] | None = None
    ) -> None:
        self._results = iter(results or [])
        self.spoken: list[str] = []
        self.cancelled = False

    def speak(self, text: str) -> SpeechResult:
        self.spoken.append(text)
        try:
            result = next(self._results)
        except StopIteration:
            return SpeechResult(True)
        if isinstance(result, Exception):
            raise result
        return result

    def cancel(self) -> None:
        self.cancelled = True

    def health(self) -> dict[str, str]:
        return {"status": "available", "provider": "fake"}
