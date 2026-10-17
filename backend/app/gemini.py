"""Thin async wrapper over the Google Gen AI SDK on Vertex AI, returning validated
pydantic objects. Structured output is enforced with a JSON schema."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import Settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
QUOTA_PAUSE_MAX_S = 3600   # after a quota error, skip Gemini until the reported retry time (at most 1 h)


class GeminiError(Exception):
    """str() is a short reason safe to show users; the full API error goes to the log only."""


def _reason(e: Exception) -> str:
    s = str(e)
    if isinstance(e, asyncio.TimeoutError):
        return "timed out"
    if isinstance(e, (ValidationError, json.JSONDecodeError)):
        return "returned an unreadable answer"
    if "RESOURCE_EXHAUSTED" in s or s.startswith("429"):
        return "quota used up"
    if "UNAVAILABLE" in s or s.startswith("503"):
        return "model busy"
    if "PERMISSION_DENIED" in s or "API_KEY_INVALID" in s or s.startswith(("401", "403")):
        return "access denied"
    return "request failed"


class Gemini:
    def __init__(self, s: Settings):
        from google import genai
        from google.genai import types
        self.types = types
        self.s = s
        if s.gemini_mode == "api_key":
            if not s.gemini_api_key:
                raise GeminiError("GEMINI_API_KEY is required with GEMINI_MODE=api_key")
            self.client = genai.Client(api_key=s.gemini_api_key)
        elif s.gemini_mode == "vertex":
            self.client = genai.Client(vertexai=True, project=(s.gemini_project or s.google_cloud_project) or None,
                                       location=s.gemini_location)
        else:
            raise GeminiError(f"unknown GEMINI_MODE {s.gemini_mode!r}")
        self._paused_until = 0.0

    def text_part(self, text: str):
        return self.types.Part.from_text(text=text)

    def bytes_part(self, data: bytes, mime: str):
        return self.types.Part.from_bytes(data=data, mime_type=mime)

    async def json(self, schema: type[T], system: str, parts: list, attempts: int = 2) -> T:
        if time.monotonic() < self._paused_until:
            raise GeminiError("quota used up")
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
            if _reason(last) == "quota used up":
                # Retrying can't succeed and spends quota; pause until Google says it resets.
                m = re.search(r"retryDelay['\"]?\s*:\s*['\"](\d+)s", str(last))
                self._paused_until = time.monotonic() + min(int(m.group(1)) if m else 60, QUOTA_PAUSE_MAX_S)
                break
            await asyncio.sleep(0.6 * (i + 1))
        raise GeminiError(_reason(last) if last else "request failed")
