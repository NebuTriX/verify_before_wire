"""Thin async wrapper over the Google Gen AI SDK on Vertex AI, returning validated
pydantic objects. Structured output is enforced with a JSON schema."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import Settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class GeminiError(Exception):
    pass


class Gemini:
    def __init__(self, s: Settings):
        from google import genai
        from google.genai import types
        self.types = types
        self.s = s
        self.client = genai.Client(vertexai=True, project=s.google_cloud_project or None,
                                   location=s.gemini_location)

    def text_part(self, text: str):
        return self.types.Part.from_text(text=text)

    def bytes_part(self, data: bytes, mime: str):
        return self.types.Part.from_bytes(data=data, mime_type=mime)

    async def json(self, schema: type[T], system: str, parts: list, attempts: int = 2) -> T:
        t = self.types
        cfg = dict(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
            seed=self.s.gemini_seed,
        )
        if self.s.gemini_temperature is not None:
            cfg["temperature"] = self.s.gemini_temperature
        if self.s.gemini_thinking_level:
            cfg["thinking_config"] = t.ThinkingConfig(thinking_level=self.s.gemini_thinking_level)
        config = t.GenerateContentConfig(**cfg)
        last: Exception | None = None
        for i in range(attempts):
            try:
                resp = await asyncio.wait_for(
                    self.client.aio.models.generate_content(
                        model=self.s.gemini_model,
                        contents=[t.Content(role="user", parts=parts)],
                        config=config),
                    timeout=self.s.gemini_timeout_s)
                return schema.model_validate(json.loads(resp.text or "{}"))
            except (asyncio.TimeoutError, ValidationError, json.JSONDecodeError) as e:
                last = e
            except Exception as e:  # SDK / API errors
                last = e
            log.warning("Gemini attempt %d failed: %s", i + 1, last)
            await asyncio.sleep(0.6 * (i + 1))
        raise GeminiError(str(last))
