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
        )
