"""Environment-backed configuration for Cato."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required configuration is unavailable or invalid."""


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None
    gemini_model: str
    approved_roots: tuple[Path, ...]
    log_level: str
    max_agent_iterations: int = 8
    max_file_read_bytes: int = 1_000_000
    max_command_output_bytes: int = 20_000
    command_timeout: float = 10.0
    history_limit: int = 50
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    approval_ttl_seconds: int = 300
    max_clipboard_bytes: int = 100_000
    allowed_macos_apps: tuple[str, ...] = (
        "Finder",
        "Safari",
        "Terminal",
        "Visual Studio Code",
        "Notes",
    )
    voice_enabled: bool = True
    stt_provider: str = "faster-whisper"
    stt_model: str = "base"
    stt_timeout_seconds: float = 120.0
    stt_device: str = "cpu"
    stt_compute_type: str = "auto"
    tts_provider: str = "macos-say"
    microphone_device: str | None = None
    recording_timeout_seconds: float = 12.0
    silence_timeout_seconds: float = 1.2
    silence_threshold: float = 500.0
    audio_block_size: int = 1024
    tts_voice: str | None = None
    tts_rate: int | None = None
    max_spoken_characters: int = 500
    voice_client_api_url: str = "http://127.0.0.1:8000"

    @classmethod
    def load(cls, *, require_api_key: bool = True) -> Settings:
        load_dotenv(override=False)
        api_key = os.getenv("GEMINI_API_KEY") or None
        if require_api_key and not api_key:
            raise ConfigurationError(
                "GEMINI_API_KEY is not configured. Copy .env.example to .env "
                "and add your key."
            )

        raw_roots = os.getenv("CATO_APPROVED_ROOTS", ".")
        roots = tuple(
            Path(value).expanduser().resolve()
            for value in raw_roots.split(os.pathsep)
            if value.strip()
        )
        if not roots:
            raise ConfigurationError("CATO_APPROVED_ROOTS must contain a path.")

        return cls(
            gemini_api_key=api_key,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
            approved_roots=roots,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            max_agent_iterations=_integer(
                "CATO_MAX_AGENT_ITERATIONS", 8, minimum=1, maximum=50
            ),
            max_file_read_bytes=_integer(
                "CATO_MAX_FILE_READ_BYTES", 1_000_000, minimum=1024
            ),
            max_command_output_bytes=_integer(
                "CATO_MAX_COMMAND_OUTPUT_BYTES", 20_000, minimum=1024
            ),
            command_timeout=_float("CATO_COMMAND_TIMEOUT", 10, minimum=0.1),
            history_limit=_integer("CATO_HISTORY_LIMIT", 50, minimum=4),
            api_host=os.getenv("CATO_API_HOST", "127.0.0.1"),
            api_port=_integer("CATO_API_PORT", 8000, minimum=1, maximum=65535),
            approval_ttl_seconds=_integer(
                "CATO_APPROVAL_TTL_SECONDS", 300, minimum=1, maximum=3600
            ),
            max_clipboard_bytes=_integer(
                "CATO_MAX_CLIPBOARD_BYTES", 100_000, minimum=1
            ),
            allowed_macos_apps=tuple(
                item.strip()
                for item in os.getenv(
                    "CATO_ALLOWED_MACOS_APPS",
                    "Finder,Safari,Terminal,Visual Studio Code,Notes",
                ).split(",")
                if item.strip()
            ),
            voice_enabled=_boolean("CATO_VOICE_ENABLED", True),
            stt_provider=os.getenv("CATO_STT_PROVIDER", "faster-whisper"),
            stt_model=os.getenv("CATO_STT_MODEL", "base"),
            stt_timeout_seconds=_float("CATO_STT_TIMEOUT_SECONDS", 120, minimum=0.1),
            stt_device=os.getenv("CATO_STT_DEVICE", "cpu"),
            stt_compute_type=os.getenv("CATO_STT_COMPUTE_TYPE", "auto"),
            tts_provider=os.getenv("CATO_TTS_PROVIDER", "macos-say"),
            microphone_device=os.getenv("CATO_MICROPHONE_DEVICE") or None,
            recording_timeout_seconds=_float(
                "CATO_RECORDING_TIMEOUT_SECONDS", 12, minimum=0.1
            ),
            silence_timeout_seconds=_float(
                "CATO_SILENCE_TIMEOUT_SECONDS", 1.2, minimum=0.1
            ),
            silence_threshold=_float("CATO_SILENCE_THRESHOLD", 500, minimum=1),
            audio_block_size=_integer("CATO_AUDIO_BLOCK_SIZE", 1024, minimum=128),
            tts_voice=os.getenv("CATO_TTS_VOICE") or None,
            tts_rate=_optional_integer("CATO_TTS_RATE", minimum=80, maximum=500),
            max_spoken_characters=_integer(
                "CATO_MAX_SPOKEN_CHARACTERS", 500, minimum=80
            ),
            voice_client_api_url=os.getenv(
                "CATO_VOICE_CLIENT_API_URL", "http://127.0.0.1:8000"
            ),
        )


def _integer(
    name: str, default: int, *, minimum: int, maximum: int | None = None
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer.") from error
    if value < minimum or maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} is outside its allowed range.")
    return value


def _float(name: str, default: float, *, minimum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a number.") from error
    if value < minimum:
        raise ConfigurationError(f"{name} is outside its allowed range.")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _optional_integer(
    name: str, *, minimum: int, maximum: int | None = None
) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer.") from error
    if parsed < minimum or maximum is not None and parsed > maximum:
        raise ConfigurationError(f"{name} is outside its allowed range.")
    return parsed
