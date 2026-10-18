"""HTTP side of AI verification calls (see app/calls.py for the rules that keep them factual).

  POST /api/t/{tid}/cases/{cid}/ai-calls                        start a call to the vendor on file
  POST /api/t/{tid}/cases/{cid}/ai-calls/{call}/recording       browser channel: upload the recorded answers
  POST /api/t/{tid}/cases/{cid}/ai-calls/{call}/accept          a person accepts it as the required callback
  GET  /api/t/{tid}/cases/{cid}/ai-calls/{call}/recording       play it back
  POST /api/telephony/twilio/{tid}/{cid}/{call}/...             Twilio webhooks (real phone calls)

Channels: "twilio" places a real phone call when TWILIO_* settings are present; otherwise "browser":
the agent speaks the questions in the browser and records the answers from the microphone (used for
demos, or when an analyst is on the phone with the vendor via speaker).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from . import calls, media
from .auth import access
from .gemini import GeminiError
from .models import AiCall, CallbackRecord, Case, Decision
from .orchestrator import Deps
from .tenancy import Perm

log = logging.getLogger(__name__)
router = APIRouter(tags=["AI verification calls"])
MAX_RECORDING = 15 * 1024 * 1024
CHANNEL_LABEL = {"twilio": "phone network", "browser": "browser"}


def _deps(r: Request) -> Deps:
    return r.app.state.deps


def _twilio_ready(d: Deps) -> bool:
    s = d.settings
    return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number and s.public_base_url)


def _call(case: Case, call_id: str) -> AiCall:
    c = next((x for x in case.workflow.ai_calls if x.id == call_id), None)
    if c is None:
        raise HTTPException(404, "Call not found.")
    return c


async def _save_call(d: Deps, tid: str, cid: str, call: AiCall) -> Case:
    def fn(case: Case):
        case.workflow.ai_calls = [call if x.id == call.id else x for x in case.workflow.ai_calls]
    res = await d.store.update_case(tid, cid, fn)
    if res is None:
        raise HTTPException(404, "Case not found.")
    return res[0]


# --------------------------------------------------------------- start ----
@router.post("/api/t/{tid}/cases/{cid}/ai-calls", status_code=201)
async def start_call(tid: str, cid: str, request: Request):
    a = await access(request, tid, Perm.callback_record)
    d = _deps(request)
    case = await d.store.get_case(tid, cid)
    if not case or not case.verdict:
        raise HTTPException(409, "The investigation hasn't finished.")
    if case.verdict.decision == Decision.release:
        raise HTTPException(409, "This payment wasn't stopped, so no verification call is needed.")
    if case.workflow.state not in ("awaiting_decision", "awaiting_second_approval"):
        raise HTTPException(409, f"The case is already {case.workflow.state}.")
    vendor = calls.vendor_for(case, await d.store.list_vendors(tid))
    if vendor is None or not vendor.callback_phone:
        raise HTTPException(409, "This payee isn't in your vendor master, so there is no trusted number to call. "
                                 "Call the requester through your company directory instead.")
    call = calls.new_call(case, vendor, "twilio" if _twilio_ready(d) else "browser", a.principal.uid)
    res = await d.store.update_case(tid, cid, lambda c: c.workflow.ai_calls.append(call))
    if res is None:
        raise HTTPException(404, "Case not found.")
    await d.store.audit(tid, a.principal.actor, "case.ai_call_started", f"case/{cid}",
                        {"call": call.id, "number": call.number, "number_source": "vendor_master", "channel": call.channel})
    if call.channel == "twilio":
        try:
            call.provider_ref = await _twilio_dial(d, tid, cid, call)
            call.status = "in_progress"
        except httpx.HTTPError as e:
            call.status, call.error = "failed", f"Twilio could not place the call: {e}"[:300]
        await _save_call(d, tid, cid, call)
    return call.model_dump(mode="json", exclude={"recording_uri"})


# --------------------------------------------------- browser recording ----
@router.post("/api/t/{tid}/cases/{cid}/ai-calls/{call_id}/recording")
async def upload_recording(tid: str, cid: str, call_id: str, request: Request, file: UploadFile = File(...),
                           browser_answers: str = Form("[]"), marks: str = Form("[]")):
    a = await access(request, tid, Perm.callback_record)
    d = _deps(request)
    case = await d.store.get_case(tid, cid)
    if not case:
        raise HTTPException(404, "Case not found.")
    call = _call(case, call_id)
    if call.status in ("completed", "analysing"):
        raise HTTPException(409, "This call already has a recording.")
    raw = await file.read(MAX_RECORDING + 1)
    if len(raw) > MAX_RECORDING:
        raise HTTPException(413, "The recording is larger than 15 MB.")
    try:
        answers = [str(x)[:1000] for x in json.loads(browser_answers or "[]")][: len(call.answers)]
        times = [float(x) for x in json.loads(marks or "[]")][: len(call.answers)]
    except (ValueError, TypeError):
        raise HTTPException(422, "browser_answers and marks must be JSON lists.") from None
    call.status, call.second_transcript = "analysing", answers
    await _save_call(d, tid, cid, call)
    await _analyse(d, tid, cid, call, raw, (file.content_type or "audio/webm").split(";")[0], answers, times, a.principal.actor)
    return call.model_dump(mode="json", exclude={"recording_uri"})


async def _analyse(d: Deps, tid: str, cid: str, call: AiCall, raw: bytes, mime: str,
                   second: list[str], marks: list[float], actor: str) -> None:
    """Store the recording, transcribe it, keep only verified answers, derive the outcome."""
    try:
        wav = await asyncio.to_thread(media.to_wav, raw)          # one format for storage and for Gemini
    except media.MediaUnavailable:
        wav = None
    data, kind = (wav, "audio/wav") if wav else (raw, mime)
    call.recording_sha256 = hashlib.sha256(data).hexdigest()
    call.recording_mime = kind
    call.recording_uri = await d.store.put_blob(f"{tid}/cases/{cid}/calls/{call.id}", data, kind)
    if wav:
        call.duration_s = round(len(wav) / media.WAV_BYTES_PER_S, 1)
    model_answers, transcript, by = None, None, None
    if d.gemini:
        questions = "\n".join(f"{i}. {a.question}" for i, a in enumerate(call.answers))
        mark_txt = (" (question i starts at about " + ", ".join(f"{i}: {t:.0f}s" for i, t in enumerate(marks)) + ")") if marks else ""
        try:
            res = await d.gemini.json(calls.CallAnalysis, calls.ANALYSIS_PROMPT.format(questions=questions, marks=mark_txt),
                                      [d.gemini.text_part("Recorded answers follow."), d.gemini.bytes_part(data, kind)])
            model_answers, transcript, by = res.answers, res.transcript, "gemini"
        except GeminiError as e:
            call.error = f"AI transcription unavailable ({e})."
    if transcript is None:
        fallback = CHANNEL_LABEL[call.channel]
        if any(x.strip() for x in second):
            transcript, by = "\n".join(x.strip() for x in second if x.strip()), call.channel
            note = f"Used the {fallback}'s own transcript instead."
        else:
            transcript, by = "", None
            note = f"The {fallback} did not produce a transcript either."
        call.error = f"{call.error} {note}" if call.error else note
    calls.apply_answers(call, transcript, model_answers, second or None, by)
    if not transcript:
        call.outcome, call.outcome_basis = "unclear", "No transcript is available. Listen to the recording and record the callback by hand."
    call.status, call.completed_at = "completed", datetime.now(timezone.utc)
    await _save_call(d, tid, cid, call)
    await d.store.audit(tid, actor, "case.ai_call_completed", f"case/{cid}",
                        {"call": call.id, "outcome": call.outcome, "recording_sha256": call.recording_sha256,
                         "transcript_by": call.transcript_by})
    if call.outcome == "denied":
        await d.store.audit(tid, "system", "case.fraud_signal", f"case/{cid}",
                            {"reason": "vendor denied the request on a recorded verification call", "call": call.id})


@router.post("/api/t/{tid}/cases/{cid}/ai-calls/{call_id}/cancel")
async def cancel_call(tid: str, cid: str, call_id: str, request: Request):
    """Close a call that was hung up before any recording was submitted, so it never stays 'in progress'."""
    a = await access(request, tid, Perm.callback_record)
    d = _deps(request)

    def fn(case: Case):
        call = _call(case, call_id)
        if call.status in ("dialing", "in_progress"):
            call.status, call.error, call.completed_at = "failed", "Call ended before an answer was recorded.", datetime.now(timezone.utc)
        return call
    res = await d.store.update_case(tid, cid, fn)
    if res is None:
        raise HTTPException(404, "Case not found.")
    await d.store.audit(tid, a.principal.actor, "case.ai_call_cancelled", f"case/{cid}", {"call": call_id})
    return res[1].model_dump(mode="json", exclude={"recording_uri"})


# -------------------------------------------------------------- accept ----
@router.post("/api/t/{tid}/cases/{cid}/ai-calls/{call_id}/accept")
async def accept_call(tid: str, cid: str, call_id: str, request: Request):
    """A person reviewed the recording and transcript: count the call as the callback the policy needs.
    A confirmed call satisfies the callback requirement; a denied one is recorded as a denial."""
    a = await access(request, tid, Perm.callback_record)
    d = _deps(request)

    def fn(case: Case):
        call = _call(case, call_id)
        if call.status != "completed" or call.outcome not in ("confirmed", "denied"):
            raise HTTPException(409, "Only a completed call with a clear, quoted answer can be accepted.")
        if call.accepted_by:
            raise HTTPException(409, "This call was already accepted.")
        if case.workflow.state not in ("awaiting_decision", "awaiting_second_approval"):
            raise HTTPException(409, f"The case is already {case.workflow.state}.")
        call.accepted_by, call.accepted_at = a.principal.email or a.principal.uid, datetime.now(timezone.utc)
        case.workflow.callbacks.append(CallbackRecord(
            uid=a.principal.uid, email=a.principal.email, contact=call.contact, number=call.number,
            number_source="vendor_master", outcome=call.outcome,
            notes=f"AI verification call {call.id}, recording SHA-256 {call.recording_sha256}. Reviewed and accepted."))
        return call
    res = await d.store.update_case(tid, cid, fn)
    if res is None:
        raise HTTPException(404, "Case not found.")
    case, call = res
    await d.store.audit(tid, a.principal.actor, "case.callback_recorded", f"case/{cid}",
                        {"outcome": call.outcome, "number_source": "vendor_master", "via": "ai_call", "call": call.id})
    return {"state": case.workflow.state, "call": call.model_dump(mode="json", exclude={"recording_uri"})}


@router.get("/api/t/{tid}/cases/{cid}/ai-calls/{call_id}/recording")
async def get_recording(tid: str, cid: str, call_id: str, request: Request):
    await access(request, tid, Perm.case_read)
    d = _deps(request)
    case = await d.store.get_case(tid, cid)
    if not case:
        raise HTTPException(404, "Case not found.")
    call = _call(case, call_id)
    if not call.recording_uri:
        raise HTTPException(404, "No recording yet.")
    return Response(await d.store.get_blob(call.recording_uri), media_type=call.recording_mime or "audio/wav",
                    headers={"Content-Security-Policy": "sandbox; default-src 'none'; media-src 'self'"})


# -------------------------------------------------------------- Twilio ----
def twilio_signature(token: str, url: str, params: dict[str, str]) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(auth token, url + every POST param name+value, sorted by name))."""
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


async def _twilio_params(request: Request, d: Deps) -> dict[str, str]:
    form = {k: str(v) for k, v in (await request.form()).items()}
    url = d.settings.public_base_url.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not hmac.compare_digest(twilio_signature(d.settings.twilio_auth_token, url, form), request.headers.get("x-twilio-signature", "")):
        raise HTTPException(403, "Bad Twilio signature.")
    return form


def _hook(d: Deps, tid: str, cid: str, call_id: str, tail: str) -> str:
    return f"{d.settings.public_base_url.rstrip('/')}/api/telephony/twilio/{tid}/{cid}/{call_id}/{tail}"


async def _twilio_dial(d: Deps, tid: str, cid: str, call: AiCall) -> str:
    s = d.settings
    r = await d.sources.http.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Calls.json",
        auth=(s.twilio_account_sid, s.twilio_auth_token),
        data={"To": call.number, "From": s.twilio_from_number, "Url": _hook(d, tid, cid, call.id, "q/0"),
              "Record": "true", "RecordingStatusCallback": _hook(d, tid, cid, call.id, "recording"),
              "RecordingStatusCallbackEvent": "completed"})
    r.raise_for_status()
    return r.json().get("sid", "")


def _twiml(body: str) -> Response:
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', media_type="application/xml")


@router.post("/api/telephony/twilio/{tid}/{cid}/{call_id}/q/{n}", include_in_schema=False)
async def twilio_question(tid: str, cid: str, call_id: str, n: int, request: Request):
    d = _deps(request)
    form = await _twilio_params(request, d)
    case = await d.store.get_case(tid, cid)
    if not case:
        raise HTTPException(404, "Case not found.")
    call = _call(case, call_id)
    if 0 < n <= len(call.answers) and (said := form.get("SpeechResult")):   # Twilio's transcript of answer n-1
        call.second_transcript += [""] * (len(call.answers) - len(call.second_transcript))
        call.second_transcript[n - 1] = said[:1000]
        await _save_call(d, tid, cid, call)
    intro = (f"Hello, this is the automated payment verification assistant calling for "
             f"{xml_escape(d.settings.company_name)}. This call is recorded. ") if n == 0 else ""
    if n >= len(call.answers):
        return _twiml("<Say>Thank you. A colleague will review this call. Goodbye.</Say><Hangup/>")
    q = xml_escape(call.answers[n].question)
    nxt = xml_escape(_hook(d, tid, cid, call_id, f"q/{n + 1}"))
    return _twiml(f'<Gather input="speech" speechTimeout="auto" timeout="8" action="{nxt}" method="POST">'
                  f"<Say>{intro}{q}</Say></Gather><Redirect method=\"POST\">{nxt}</Redirect>")


@router.post("/api/telephony/twilio/{tid}/{cid}/{call_id}/recording", include_in_schema=False)
async def twilio_recording(tid: str, cid: str, call_id: str, request: Request):
    d = _deps(request)
    form = await _twilio_params(request, d)
    case = await d.store.get_case(tid, cid)
    if not case:
        raise HTTPException(404, "Case not found.")
    call = _call(case, call_id)
    if call.status in ("completed", "analysing"):          # Twilio retries callbacks: analyse once
        return Response(status_code=204)
    url = form.get("RecordingUrl", "")
    if not re.match(r"^https://api\.twilio\.com/", url):
        raise HTTPException(422, "Unexpected recording URL.")
    s = d.settings
    try:
        r = await d.sources.http.get(url + ".wav", auth=(s.twilio_account_sid, s.twilio_auth_token), follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not download the recording from Twilio: {type(e).__name__}") from e
    call.status = "analysing"
    await _save_call(d, tid, cid, call)
    await _analyse(d, tid, cid, call, r.content, "audio/wav", call.second_transcript, [], "system")
    return Response(status_code=204)
