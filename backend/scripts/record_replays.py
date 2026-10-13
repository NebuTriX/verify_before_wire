"""Runs each demo scenario live against a deployed service and saves the run as a
replay recording (demo/replays/<scenario>.json) for the hidden fail-safe toggle.

    python scripts/record_replays.py https://verify-before-wire-xxxx.a.run.app
Then redeploy so the recordings ship inside the container."""
import json
import sys
from pathlib import Path

import httpx

base = sys.argv[1].rstrip("/")
out = Path(__file__).resolve().parents[2] / "demo" / "replays"
out.mkdir(parents=True, exist_ok=True)
with httpx.Client(timeout=120) as c:
    for sc in c.get(f"{base}/api/demo").json():
        case_id = c.post(f"{base}/api/demo/{sc['id']}").json()["case_id"]
        decision = None
        with c.stream("GET", f"{base}/api/cases/{case_id}/stream") as r:
            for line in r.iter_lines():
                if line.startswith("data:") and '"type":"verdict"' in line.replace(" ", ""):
                    decision = json.loads(line[5:])["data"]["decision"]
                if line.startswith("event: case.completed") or line.startswith("event: case.failed"):
                    break
        rec = c.get(f"{base}/api/cases/{case_id}/export").json()
        (out / f"{sc['id']}.json").write_text(json.dumps(rec, indent=1))
        print(f"{sc['id']}: {decision} -> {out / (sc['id'] + '.json')}")
