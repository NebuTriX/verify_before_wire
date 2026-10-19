"""Checks that this installation is wired to your Firebase / Google Cloud project, and says exactly
what to fix. Reads backend/.env like the app does.

    python scripts/firebase_check.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import Settings  # noqa: E402

OK, WARN, BAD = "✓", "!", "✗"


def line(mark: str, title: str, detail: str = "") -> None:
    print(f"  {mark} {title}" + (f"\n      {detail}" if detail else ""))


async def main() -> int:
    s = Settings()
    pid = s.firebase_project_id or s.google_cloud_project
    bad = 0
    print(f"\nVerify-Before-Wire setup check  (auth={s.auth_mode}, store={s.store_backend}, gemini={s.gemini_mode})\n")

    print("Firebase Authentication")
    if s.auth_mode != "firebase":
        line(WARN, "AUTH_MODE is not 'firebase'", "Set AUTH_MODE=firebase in backend/.env to use Firebase sign-in.")
    if not pid:
        bad += 1; line(BAD, "No project id", "Set FIREBASE_PROJECT_ID (Firebase Console → Project settings → General → Project ID).")
    else:
        line(OK, f"Project id: {pid}")
    if not s.firebase_api_key:
        bad += 1; line(BAD, "No FIREBASE_API_KEY", "Copy apiKey from Firebase Console → Project settings → Your apps → Web app.")
    else:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://www.googleapis.com/identitytoolkit/v3/relyingparty/getProjectConfig",
                            params={"key": s.firebase_api_key})
        d = r.json()
        if "error" in d:
            bad += 1
            line(BAD, "This key cannot be used for Firebase sign-in", d["error"].get("message", "")[:140] +
                 "\n      Use the Web app apiKey from Firebase Console → Project settings → Your apps "
                 "(Gemini keys from AI Studio are restricted to the Gemini API).")
        else:
            if pid and d.get("projectId") != pid:
                bad += 1; line(BAD, f"Key belongs to project {d.get('projectId')}, not {pid}")
            else:
                line(OK, f"Web API key is valid for {d.get('projectId')}")
            doms = d.get("authorizedDomains", [])
            line(OK if "localhost" in doms else WARN, f"Authorized domains: {', '.join(doms) or 'none'}",
                 "" if "localhost" in doms else "Add localhost (and your deployed domain) in Authentication → Settings.")
            line(OK if d.get("allowPasswordUser") else WARN, "Email/password sign-in " + ("enabled" if d.get("allowPasswordUser") else "disabled"),
                 "" if d.get("allowPasswordUser") else "Enable it in Authentication → Sign-in method.")

    print("\nDatabase")
    if s.store_backend == "firestore":
        creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if creds and not Path(creds).exists():
            bad += 1; line(BAD, f"GOOGLE_APPLICATION_CREDENTIALS points to a missing file: {creds}")
        try:
            from app.blobs import build_blobs
            from app.store import FirestoreStore
            st = FirestoreStore(s, build_blobs(s))
            await asyncio.wait_for(st.ping(), 10)
            await st.set("_healthcheck/probe", {"ok": True})
            line(OK, f"Firestore reachable and writable (project {s.google_cloud_project or pid})")
        except Exception as e:
            bad += 1
            line(BAD, f"Firestore not reachable: {type(e).__name__}: {str(e)[:160]}",
                 "Create a service account key (Firebase Console → Project settings → Service accounts → Generate new "
                 "private key), save it outside the repo and set GOOGLE_APPLICATION_CREDENTIALS=/path/key.json and "
                 "GOOGLE_CLOUD_PROJECT. Create the Firestore database in Native mode first.")
        if s.blob_backend not in ("gcs", "memory", "file"):
            line(WARN, f"BLOB_BACKEND={s.blob_backend}")
        if s.blob_backend == "gcs" and not s.uploads_bucket:
            bad += 1; line(BAD, "BLOB_BACKEND=gcs needs UPLOADS_BUCKET", "e.g. <project>.firebasestorage.app, or use BLOB_BACKEND=file locally.")
    else:
        line(OK, f"STORE_BACKEND={s.store_backend} (not Firestore)")

    print("\nGemini")
    try:
        from app.gemini import Gemini
        st = await Gemini(s).probe()
        if st["state"] == "ok":
            line(OK, f"Gemini answers ({s.gemini_mode}, {s.gemini_model})")
        else:
            bad += 1
            line(BAD, f"Gemini {st['state']}: {st['reason']}", (f"Fix: {st['fix']['label']} → {st['fix']['url']}" if st.get("fix") else "") +
                 ("\n      Or switch to GEMINI_MODE=vertex with the same service account (billed to the project, "
                  "no prepaid credits; needs the Vertex AI API enabled)." if s.gemini_mode == "api_key" else ""))
    except Exception as e:
        bad += 1; line(BAD, f"Gemini not configured: {e}")
    if s.gemini_api_key and s.gemini_api_key == s.firebase_api_key:
        line(WARN, "GEMINI_API_KEY is the same as the public Firebase web key",
             "The web key is sent to every browser. Use a separate, server-only Gemini key or Vertex AI.")
    print(f"\n{'All good.' if not bad else f'{bad} thing(s) to fix above.'}\n")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
