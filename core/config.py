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
