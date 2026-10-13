"""Records each sample scenario from a deployed service for the Shift+R replay fail-safe.

    python scripts/record_replays.py <service-url> <workspace-id> <api-key>
Use an API key with the analyst role. Then redeploy so recordings ship in the container."""
import json
import sys
from pathlib import Path

import httpx

base, tid, key = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
h = {"Authorization": f"Bearer {key}"}
out = Path(__file__).resolve().parents[2] / "demo" / "replays"
out.mkdir(parents=True, exist_ok=True)
with httpx.Client(timeout=180, headers=h) as c:
    for sc in c.get(f"{base}/api/demo").json():
        cid = c.post(f"{base}/api/t/{tid}/demo/{sc['id']}").raise_for_status().json()["case_id"]
        with c.stream("GET", f"{base}/api/t/{tid}/cases/{cid}/stream") as r:
            for line in r.iter_lines():
                if line.startswith("event: case.completed") or line.startswith("event: case.failed"):
                    break
        rec = c.get(f"{base}/api/t/{tid}/cases/{cid}/export").raise_for_status().json()
        (out / f"{sc['id']}.json").write_text(json.dumps(rec, indent=1))
        print(f"{sc['id']}: {rec['case']['verdict']['decision']}")
