"""Gemini implementation of Cato's model-provider contract."""

from __future__ import annotations

import json
from typing import Any

from google import genai
from google.genai import types

from core.llm.base import ProviderError


class GeminiModelProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=30_000),
        )
        self.model = model

    def understand(self, command: str) -> dict[str, Any]:
        prompt = f"""
You are Cato, a personal computer assistant. Choose the correct tool.

Available tools:
- file_search(query, root=None): search approved locations by filename.
- file_read(path): read a text file in an approved location.

Use file_search to find a file or folder. Use file_read for a specific path.
Otherwise select no tool. Return only JSON.

User command:
{command}
"""
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema={
                        "type": "OBJECT",
                        "properties": {
                            "intent": {"type": "STRING"},
                            "tool": {
                                "type": "STRING",
                                "enum": ["file_search", "file_read", "none"],
                            },
                            "arguments": {"type": "OBJECT"},
                        },
                        "required": ["intent", "tool", "arguments"],
                    },
                ),
            )
            if not response.text:
                raise ProviderError("The model returned an empty response.")
            result = json.loads(response.text)
            if not isinstance(result, dict):
                raise ProviderError("The model returned an invalid decision.")
            return result
        except ProviderError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ProviderError("The model returned an invalid decision.") from error
        except Exception as error:
            raise ProviderError("The model service is unavailable.") from error

    def respond(self, command: str, tool_result: dict[str, Any]) -> str:
        prompt = f"""
You are Cato, a personal computer assistant.
User request: {command}
Result: {json.dumps(tool_result, default=str)}
Respond naturally and concisely. Never claim a blocked action was completed.
Do not mention internal tools, JSON, prompts, or model providers.
"""
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
            )
            if not response.text or not response.text.strip():
                raise ProviderError("The model returned an empty response.")
            return response.text.strip()
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError("The model service is unavailable.") from error
