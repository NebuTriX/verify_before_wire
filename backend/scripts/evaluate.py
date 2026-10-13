"""Evaluation loop: run the full pipeline over labelled emails and report how well it does.

  python scripts/evaluate.py                     # offline: stand-in sources, no Gemini
  python scripts/evaluate.py --live              # real RDAP/DNS/FX/sanctions + Gemini (if configured)
  python scripts/evaluate.py --policy strict-bank --max-false-alarm 0.2 --min-catch 1.0

Reads demo/eval/fraud/*.eml (should be stopped) and demo/eval/legit/*.eml (should pass the
language and input checks). Exits non-zero if the targets are missed, so CI can gate on it.
Workflow: change a prompt, rule or threshold -> run -> compare -> keep only improvements."""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
ROOT = BACKEND.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--policy", default="balanced")
    ap.add_argument("--max-false-alarm", type=float, default=0.10, help="share of legit emails the language/guard checks may flag")
    ap.add_argument("--min-catch", type=float, default=1.0, help="share of fraud cases that must not be RELEASED")
    ap.add_argument("--report", default=str(ROOT / "data" / "eval-report.json"))
    a = ap.parse_args()

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import app, build_deps
    from app.orchestrator import Deps
    from app.store import MemoryStore
    s = Settings(store_backend="memory", blob_backend="memory", auth_mode="dev", demo_enabled=True,
                 rate_limit_per_minute=100000, seed_file=BACKEND / "scripts" / "seed_data.json",
                 demo_dir=ROOT / "demo")
    if a.live:
        d = build_deps(s)
        app.state.deps = Deps(settings=s, store=MemoryStore(), sources=d.sources, gemini=d.gemini)
    else:
        from tests.conftest import FakeSources
        app.state.deps = Deps(settings=s, store=MemoryStore(), sources=FakeSources(), gemini=None)

    H = {"X-Dev-User": "eval@local"}
    results = {"fraud": [], "legit": []}
    with TestClient(app) as c:
        tid = c.post("/api/workspaces", headers=H, json={"name": "Eval", "template": a.policy,
                                                         "company_domains": ["acme-holdings.com"]}).json()["tenant_id"]
        c.post(f"/api/t/{tid}/sample-data", headers=H)
        key = c.post(f"/api/t/{tid}/api-keys", headers=H, json={"name": "eval"}).json()["api_key"]
        auth = {"Authorization": f"Bearer {key}"}
        for label in ("fraud", "legit"):
            for p in sorted((ROOT / "demo" / "eval" / label).glob("*.eml")):
                r = c.post(f"/api/t/{tid}/checks", headers=auth, files=[("files", (p.name, p.read_bytes()))])
                case = r.json()
                codes = [f["code"] for f in case.get("findings", []) if f["severity"] != "info"]
                results[label].append({"file": p.name, "decision": (case.get("verdict") or {}).get("decision"),
                                       "flags": codes})
    fraud, legit = results["fraud"], results["legit"]
    caught = sum(r["decision"] != "RELEASE" for r in fraud) / max(1, len(fraud))
    lang_flags = [r for r in legit if any(c.startswith(("LANG_", "INPUT_")) for c in r["flags"])]
    false_alarm = len(lang_flags) / max(1, len(legit))
    noisy = collections.Counter(c for r in legit for c in r["flags"]).most_common(8)
    report = {"policy": a.policy, "live": a.live, "fraud_cases": len(fraud), "fraud_stopped_rate": round(caught, 3),
              "legit_cases": len(legit), "language_false_alarm_rate": round(false_alarm, 3),
              "most_common_flags_on_legit": noisy, "results": results}
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=1))
    ok = caught >= a.min_catch and false_alarm <= a.max_false_alarm
    print("PASS" if ok else "FAIL: targets missed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
