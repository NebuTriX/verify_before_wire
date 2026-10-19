"""Voice & video forensics agent: is a voice note or video message likely synthetic or doctored?

Deterministic signals come from analysing the file on this server (app/media.py); Gemini, when
available, adds an indicative review of what it hears and sees. Deepfake detection is an arms race
and no detector is reliable, so these findings are capped at *medium* severity: they raise
attention, while the decision rests on verifying the request itself (account on file, callback to
the number in the vendor master). We verify the request, not the voice."""
from __future__ import annotations

import asyncio
import time
from typing import Literal

from pydantic import BaseModel, Field

from .. import media, trace
from ..gemini import GeminiError
from ..models import Evidence, Finding, Severity
from ..parsing import MEDIA, VIDEO
from .base import AgentContext, Emit

NAME = "media"
TITLE = "Voice & video forensics"
SRC = "Media forensics (on this server)"
STATIC_MOTION = 0.002          # mean frame-to-frame change below this = a still picture
SILENT_SHARE = 0.05            # >= 5% of 20 ms frames at exact digital zero
SILENT_FLOOR_DB = -100.0
MIN_SPEECH_SHARE = 0.1         # someone is talking for at least 10% of the clip


class MediaReview(BaseModel):
    synthetic_likelihood: Literal["low", "medium", "high", "unknown"] = Field(
        description="How likely the recording is AI-generated or manipulated. 'unknown' if you cannot tell.")
    observations: list[str] = Field(default_factory=list, max_length=4,
                                    description="Concrete, checkable artifacts you noticed (max 4, one sentence each).")


REVIEW_PROMPT = """You are a cautious media-forensics reviewer at a bank. Listen to (and, for video, watch) the
attached recording, which asks for a payment. Assess only whether it shows signs of being synthetic or
manipulated: robotic or unnaturally even prosody, missing breaths, identical noise floor, abrupt splices,
mispronounced names, lip movements that do not match speech, a static face, unnatural blinking, warped
edges or lighting. Do not judge the payment request itself. Most real recordings are imperfect; only say
'high' when several clear artifacts are present, and 'unknown' when the evidence is thin."""


async def run(ctx: AgentContext, emit: Emit) -> None:
    docs = [d for d in ctx.docs if d.mime_type in MEDIA]
    if not docs:
        await emit(Finding(agent=NAME, code="NO_MEDIA", severity=Severity.info, title="No voice note or video to analyse",
                           detail="Only documents and text were provided.", data_source="Payment request"))
        return
    for d in docs:
        noun = "video" if d.mime_type in VIDEO else "voice note"
        t0 = time.perf_counter()
        try:
            rep = await asyncio.to_thread(media.analyse, ctx.blobs[d.label])
        except media.MediaUnavailable as e:
            await trace.record("forensics", d.filename, "error", str(e), int((time.perf_counter() - t0) * 1000))
            await emit(Finding(agent=NAME, code="MEDIA_UNANALYSED", severity=Severity.info, title=f"The {noun} could not be analysed",
                               detail=f"{e}. The request is still verified against your records.", data_source=SRC))
            continue
        await trace.record("forensics", d.filename, "ok", rep.summary(), int((time.perf_counter() - t0) * 1000))
        noun = "video" if rep.kind == "video" else "voice note"          # what the file really contains
        ev = [Evidence(source=d.label, excerpt=rep.summary())]
        flagged = False

        if rep.synth_tool:
            flagged = True
            await emit(Finding(agent=NAME, code="SYNTH_TOOL_METADATA", severity=Severity.medium,
                               title=f"The {noun} was produced with '{rep.synth_tool}'",
                               detail=f"The file's own metadata names a voice/video generation tool ({rep.synth_tool}).",
                               evidence=[Evidence(source=d.label, excerpt="; ".join(f"{k}={v}" for k, v in rep.tags.items())[:240])],
                               data_source=SRC, meta={"tags": rep.tags}))
        if rep.silent_ratio is not None and rep.silent_ratio >= SILENT_SHARE and (rep.noise_floor_db or 0) <= SILENT_FLOOR_DB:
            flagged = True
            await emit(Finding(agent=NAME, code="DIGITAL_SILENCE", severity=Severity.medium,
                               title=f"The {noun} has perfectly silent gaps",
                               detail=f"{rep.silent_ratio:.0%} of the audio is exact digital silence, with no room noise at all. "
                                      "A real microphone always picks up some background; synthetic or spliced speech often "
                                      "does not. (Some apps' noise suppression can do this too.)",
                               evidence=ev, data_source=SRC,
                               meta={"silent_ratio": rep.silent_ratio, "noise_floor_db": rep.noise_floor_db}))
        if rep.kind == "video" and rep.motion is not None and rep.motion < STATIC_MOTION and (rep.speech_ratio or 0) > MIN_SPEECH_SHARE:
            flagged = True
            await emit(Finding(agent=NAME, code="STATIC_VIDEO", severity=Severity.medium,
                               title="The video is a still picture with a voice-over",
                               detail="Someone is speaking, but the picture does not move at all: no lips, no blinking, no "
                                      "camera shake. This is the cheapest way to fake a video message from a photo.",
                               evidence=ev, data_source=SRC, meta={"motion": rep.motion}))
        if rep.kind == "video" and not rep.device and not flagged:
            await emit(Finding(agent=NAME, code="NO_DEVICE_METADATA", severity=Severity.info,
                               title="The video carries no recording-device details",
                               detail="Phone recordings usually name the device; this file was re-encoded or generated by "
                                      "software. Common for files forwarded through chat apps, so not a risk signal alone.",
                               evidence=ev, data_source=SRC))

        # Indicative AI review (never more than a medium signal)
        if ctx.gemini and ctx.settings.media_ai_review:
            try:
                review = await ctx.gemini.json(MediaReview, REVIEW_PROMPT, [
                    ctx.gemini.text_part(f"Recording: {d.filename} ({rep.summary()})"),
                    ctx.gemini.bytes_part(ctx.blobs[d.label], d.mime_type)])
            except GeminiError as e:
                await emit(Finding(agent=NAME, code="MEDIA_AI_REVIEW_UNAVAILABLE", severity=Severity.info,
                                   title="AI media review unavailable", detail=f"{e}. The on-server checks above still ran.",
                                   data_source="Gemini media review"))
            else:
                sev = {"high": Severity.medium, "medium": Severity.low}.get(review.synthetic_likelihood, Severity.info)
                if sev != Severity.info:
                    flagged = True
                obs = [o for o in review.observations if o.strip()][:4]
                await emit(Finding(agent=NAME, code=f"MEDIA_AI_{review.synthetic_likelihood.upper()}", severity=sev,
                                   title=f"AI review: {review.synthetic_likelihood} likelihood the {noun} is synthetic",
                                   detail=(" ".join(obs) or "No specific artifacts noted.") +
                                          " Indicative only; AI deepfake detection is not reliable on its own.",
                                   evidence=[Evidence(source=d.label, excerpt=o) for o in obs[:2]],
                                   data_source="Gemini media review (indicative)"))
        if not flagged:
            await emit(Finding(agent=NAME, code="MEDIA_NO_SIGNALS", severity=Severity.info,
                               title=f"No signs of synthesis found in the {noun}",
                               detail=f"{rep.summary()}. " + "; ".join(x for x in (
                                          "natural room noise present" if rep.silent_ratio is not None else "",
                                          "moving picture" if rep.kind == "video" and rep.motion is not None else "") if x).capitalize()
                                      + ". A convincing deepfake can still pass, "
                                      "which is why the request is verified independently.", evidence=ev, data_source=SRC))
