"""Backward-compatible name for the original Gemini-backed brain."""

from core.config import Settings
from core.llm.gemini import GeminiModelProvider


class CatoBrain(GeminiModelProvider):
    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or Settings.load()
        assert settings.gemini_api_key is not None
        super().__init__(settings.gemini_api_key, settings.gemini_model)
