"""HTTP API + static frontend. One Cloud Run service serves both, so the event stream
is same-origin and never buffered by an intermediate CDN."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings
from .models import Case, CaseStatus, Event
from .orchestrator import TERMINAL, Deps, Publisher, investigate
from .parsing import UnsupportedFile, _Labeler, parse_upload
from .report import build_html, build_pdf
from .sources import Sources
from .store import build_store

log = logging.getLogger("vbw")
SCENARIOS_FILE = "scenarios.json"


def build_deps(s: Settings) -> Deps:
    gemini = None
    if s.google_cloud_project or s.store_backend == "firestore":
        try:
            from .gemini import Gemini
            gemini = Gemini(s)
        except Exception as e:  # missing credentials locally
            log.warning("Gemini disabled: %s", e)
    return Deps(settings=s, store=build_store(s), sources=Sources.build(s), gemini=gemini)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(levelname)s %(name)s %(message)s")
    if not hasattr(app.state, "deps"):
        app.state.deps = build_deps(s)
    yield
    await app.state.deps.sources.aclose()


app = FastAPI(title="Verify-Before-Wire", version="1.0.0", lifespan=lifespan)


def deps(request: Request) -> Deps:
    return request.app.state.deps


# ------------------------------------------------------------------ cases ----
async def _create_case(d: Deps, files: list[tuple[str, str | None, bytes]], scenario: str | None) -> Case:
    s = d.settings
    if not files:
        raise HTTPException(400, "Add at least one file: an email (.eml), voice note, invoice or text.")
    if len(files) > s.max_files:
        raise HTTPException(400, f"Upload at most {s.max_files} files.")
    case = Case(id=uuid.uuid4().hex[:12], scenario=scenario)
    label = _Labeler()
    for name, mime, data in files:
        if len(data) > s.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"{name} is larger than {s.max_upload_mb} MB.")
        try:
            for doc, raw in parse_upload(name, mime, data, label):
                doc.storage_uri = await d.store.put_blob(f"cases/{case.id}/{doc.label}", raw, doc.mime_type)
                case.sources.append(doc)
        except UnsupportedFile as e:
            raise HTTPException(415, str(e))
    await d.store.create_case(case)
    return case


@app.post("/api/cases", status_code=201)
async def create_case(request: Request, files: list[UploadFile] = File(...)):
    d = deps(request)
    payload = [(f.filename or "upload", f.content_type, await f.read()) for f in files]
    case = await _create_case(d, payload, None)
    return {"case_id": case.id, "inputs": [s.label for s in case.sources]}


def _scenarios(s: Settings) -> list[dict]:
    p = s.demo_dir / SCENARIOS_FILE
    return json.loads(p.read_text()) if p.exists() else []


@app.get("/api/demo")
async def list_demo(request: Request):
    s = deps(request).settings
    out = []
    for sc in _scenarios(s):
        out.append({**{k: sc[k] for k in ("id", "title", "description")},
                    "files": [f for f in sc["files"] if (s.demo_dir / "files" / f).exists()],
                    "has_replay": (s.demo_dir / "replays" / f"{sc['id']}.json").exists()})
    return out


@app.post("/api/demo/{scenario_id}", status_code=201)
async def run_demo(scenario_id: str, request: Request):
    d = deps(request)
    sc = next((x for x in _scenarios(d.settings) if x["id"] == scenario_id), None)
    if not sc:
        raise HTTPException(404, "Unknown scenario")
    files = []
    for f in sc["files"]:
        p: Path = d.settings.demo_dir / "files" / f
        if p.exists():
            files.append((p.name, None, p.read_bytes()))
    case = await _create_case(d, files, scenario_id)
    return {"case_id": case.id, "inputs": [s.label for s in case.sources]}


@app.get("/api/cases/{case_id}")
async def get_case(case_id: str, request: Request):
    case = await deps(request).store.get_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    return case.model_dump(mode="json", exclude={"sources": {"__all__": {"storage_uri"}}})


def _sse(ev: Event) -> str:
    return f"id: {ev.seq}\nevent: {ev.type}\ndata: {ev.model_dump_json()}\n\n"


@app.get("/api/cases/{case_id}/stream")
async def stream(case_id: str, request: Request, replay: int = 0):
    d = deps(request)
    case = await d.store.get_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    last = int(request.headers.get("last-event-id") or 0)

    async def gen():
        yield "retry: 2000\n\n"
        if replay and case.scenario:
            async for chunk in _replay(d, case):
                yield chunk
            return
        if await d.store.try_claim(case_id, d.settings.lease_seconds):
            fresh = await d.store.get_case(case_id)
            prior = await d.store.events_after(case_id, 0)
            q: asyncio.Queue = asyncio.Queue()
            pub = Publisher(d.store, case_id, prior[-1].seq if prior else 0, q)
            task = asyncio.create_task(investigate(fresh, d, pub))
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(ev)
                if ev.type in TERMINAL:
                    break
            await task
            return
        # Someone else is running it (or it's done): tail the persisted events.
        seq, quiet = last, 0.0
        while True:
            evs = await d.store.events_after(case_id, seq)
            for ev in evs:
                seq = ev.seq
                yield _sse(ev)
                if ev.type in TERMINAL:
                    return
            if await request.is_disconnected():
                return
            quiet = 0.0 if evs else quiet + 0.4
            if quiet and quiet % 2.0 < 0.4:  # every ~2s of silence, check the case itself
                cur = await d.store.get_case(case_id)
                if cur is None or cur.status in (CaseStatus.completed, CaseStatus.failed):
                    return
                if cur.status == CaseStatus.running and cur.lease_until and \
                        cur.lease_until.timestamp() < time.time():
                    yield _sse(Event(seq=seq, type="case.stalled", data={}))
                    return
            if quiet and quiet % 15.0 < 0.4:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


async def _replay(d: Deps, case: Case):
    """Hidden demo fail-safe: replays a recorded run with its original timing."""
    p = d.settings.demo_dir / "replays" / f"{case.scenario}.json"
    if not p.exists():
        yield _sse(Event(seq=1, type="case.failed", data={"error": "No recording for this scenario"}))
        return
    rec = json.loads(p.read_text())
    snap = Case(**{**rec["case"], "id": case.id, "sources": [s.model_dump() for s in case.sources],
                   "status": "completed"})
    await d.store.save_case(snap)
    prev = 0.0
    for i, e in enumerate(rec["events"], 1):
        await asyncio.sleep(min(1.2, max(0.0, e["t"] - prev)))
        prev = e["t"]
        ev = Event(seq=i, type=e["type"], agent=e.get("agent"), data=e.get("data", {}))
        if e["type"] == "case.started":
            ev.data["case_id"] = case.id
        await d.store.append_event(case.id, ev)
        yield _sse(ev)


@app.get("/api/cases/{case_id}/export")
async def export_case(case_id: str, request: Request):
    """Recording format used by the replay fail-safe (scripts/record_replays.py)."""
    d = deps(request)
    case = await d.store.get_case(case_id)
    if not case or case.status != CaseStatus.completed:
        raise HTTPException(409, "Case is not completed")
    evs = await d.store.events_after(case_id, 0)
    t0 = evs[0].ts if evs else None
    return {"case": case.model_dump(mode="json", exclude={"sources"}),
            "events": [{"t": (e.ts - t0).total_seconds(), "type": e.type, "agent": e.agent, "data": e.data}
                       for e in evs]}


@app.get("/api/cases/{case_id}/report.pdf")
async def report_pdf(case_id: str, request: Request):
    d = deps(request)
    case = await d.store.get_case(case_id)
    if not case or not case.verdict:
        raise HTTPException(409, "The investigation has not finished yet.")
    events = await d.store.events_after(case_id, 0)
    try:
        pdf = await asyncio.to_thread(build_pdf, case, events)
    except Exception:
        log.exception("PDF build failed; serving HTML")
        return HTMLResponse(build_html(case))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="case-{case_id}.pdf"'})


@app.get("/api/cases/{case_id}/report.html", response_class=HTMLResponse)
async def report_html(case_id: str, request: Request):
    case = await deps(request).store.get_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    return build_html(case)


@app.get("/api/vendors")
async def vendors(request: Request):
    return [v.model_dump(mode="json") for v in await deps(request).store.list_vendors()]


@app.get("/healthz")
async def healthz(request: Request):
    d = deps(request)
    return {"ok": True, "model": d.settings.gemini_model, "gemini": d.gemini is not None,
            "store": d.settings.store_backend, "sanctions": d.sources.sanctions.name}


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse({"detail": "Something went wrong on our side. Try again."}, status_code=500)


_static = get_settings().static_dir
if _static.exists():
    app.mount("/assets", StaticFiles(directory=_static), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(_static / "index.html")
