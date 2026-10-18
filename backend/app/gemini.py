"""Thin async wrapper over the Google Gen AI SDK (Vertex AI or an AI Studio API key), returning
validated pydantic objects. Structured output is enforced with a JSON schema. When credits or quota
run out, calls pause and every caller degrades gracefully (see status() and docs/firebase-setup.md)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import Settings
from .trace import traced

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
QUOTA_PAUSE_MAX_S = 3600   # after a quota error, skip Gemini until the reported retry time (at most 1 h)


class GeminiError(Exception):
    """str() is a short reason safe to show users; the full API error goes to the log only.
    `paused` marks "skipped on purpose" (out of credits/quota): the caller degrades gracefully."""

    def __init__(self, msg: str, paused: bool = False):
        super().__init__(msg)
        self.paused = paused


CREDITS = "AI credits used up (top up the project at ai.studio/projects)"
TOP_UP_URL = "https://ai.studio/projects"                          # AI Studio prepaid credits
BILLING_URL = "https://console.cloud.google.com/billing"             # Vertex AI: the project's billing
AI_STUDIO_URL = "https://aistudio.google.com/"
QUOTAS_URL = "https://console.cloud.google.com/iam-admin/quotas"
QUOTA = "quota used up"
OUT_OF_QUOTA = (QUOTA, CREDITS)     # retrying cannot help until the account is topped up / resets


def _reason(e: Exception) -> str:
    s = str(e)
    if "prepayment credits" in s or s.startswith("402"):
        return CREDITS
    if isinstance(e, asyncio.TimeoutError):
        return "timed out"
    if isinstance(e, (ValidationError, json.JSONDecodeError)):
        return "returned an unreadable answer"
    if "RESOURCE_EXHAUSTED" in s or s.startswith("429"):
        return QUOTA
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
        self._paused_reason = QUOTA
        self.last_ok: float | None = None           # monotonic time of the last good answer
        self.last_error: str | None = None

    def status(self) -> dict:
        """What the UI shows: ok, paused (out of credits or quota, with how to fix it) or error.
        Out-of-credits stays "paused" after the retry timer expires until a call succeeds again."""
        timer_running = time.monotonic() < self._paused_until
        reason = self._paused_reason if timer_running else self.last_error
        if reason in OUT_OF_QUOTA:
            state = "paused"
        elif self.last_error:
            state = "error"
        else:
            state, reason = "ok", None
        fix = None
        if state == "paused":
            vertex = self.s.gemini_mode == "vertex"
            fix = ({"label": "Top up AI credits", "url": BILLING_URL if vertex else TOP_UP_URL} if reason == CREDITS
                   else {"label": "Check quota", "url": QUOTAS_URL if vertex else AI_STUDIO_URL})
        return {"state": state, "reason": reason, "fix": fix,
                "retry_in_s": max(0, int(self._paused_until - time.monotonic())) if timer_running else 0,
                "mode": self.s.gemini_mode, "model": self.s.gemini_model}

    def resume(self) -> None:
        """Forget a pause (e.g. right after topping up credits); the next call tries again."""
        self._paused_until, self.last_error = 0.0, None

    async def probe(self) -> dict:
        """One tiny request to see whether Gemini answers now."""
        self.resume()

        class Ping(BaseModel):
            ok: bool
        try:
            await self.json(Ping, "Reply with ok=true.", [self.text_part("ping")], attempts=1)
        except GeminiError:
            pass
        return self.status()

    def text_part(self, text: str):
        return self.types.Part.from_text(text=text)

    def bytes_part(self, data: bytes, mime: str):
        return self.types.Part.from_bytes(data=data, mime_type=mime)

    @traced("gemini", lambda schema, *a, **k: schema.__name__, lambda r: "structured answer received")
    async def json(self, schema: type[T], system: str, parts: list, attempts: int = 2) -> T:
        if time.monotonic() < self._paused_until:
            raise GeminiError(self._paused_reason, paused=True)
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
                out = schema.model_validate(json.loads(resp.text or "{}"))
                self.last_ok, self.last_error = time.monotonic(), None
                return out
            except Exception as e:  # timeouts, unreadable answers and SDK / API errors are all retried once
                last = e
            log.warning("Gemini attempt %d failed: %s", i + 1, last)
            if _reason(last) in OUT_OF_QUOTA:
                # Retrying can't succeed and spends quota; pause until Google says it resets.
                m = re.search(r"retryDelay['\"]?\s*:\s*['\"](\d+)s", str(last))
                self._paused_until = time.monotonic() + min(int(m.group(1)) if m else 60, QUOTA_PAUSE_MAX_S)
                self._paused_reason = _reason(last)
                break
            await asyncio.sleep(0.6 * (i + 1))
        self.last_error = _reason(last) if last else "request failed"
        raise GeminiError(self.last_error, paused=self.last_error in OUT_OF_QUOTA)
