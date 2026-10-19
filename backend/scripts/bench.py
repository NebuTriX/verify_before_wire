"""Load test: N payment checks through the real HTTP API with C in flight, end to end
(PostgreSQL, worker, live registers, policy, audit). Prints throughput and latency percentiles.

    python scripts/bench.py --n 200 --concurrency 20
Uses dev sign-in to create a throwaway workspace; point --url at any running instance. Single-payment
checks count against RATE_LIMIT_PER_MINUTE (default 60 per workspace), so raise it on the server first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

SEED = Path(__file__).resolve().parent / "seed_data.json"


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=20)
    args = ap.parse_args()
    owner = {"X-Dev-User": "bench@acme-holdings.com"}
    async with httpx.AsyncClient(base_url=args.url, timeout=120) as c:
        tpl = (await c.get("/api/templates")).json()[0]["id"]
        tid = (await c.post("/api/workspaces", headers=owner, json={
            "name": f"Benchmark {int(time.time())}", "company_domains": ["acme-holdings.com"], "base_currency": "USD",
            "template": tpl})).json()["tenant_id"]
        await c.post(f"/api/t/{tid}/sample-data", headers=owner)
        key = (await c.post(f"/api/t/{tid}/api-keys", headers=owner, json={"name": "bench", "role": "integration"})).json()["api_key"]
        auth = {"Authorization": f"Bearer {key}"}
        seed = json.loads(SEED.read_text())
        vendors = seed["vendors"]
        sem, lat, decisions, errors = asyncio.Semaphore(args.concurrency), [], {}, []

        async def one(i: int) -> None:
            v = vendors[i % len(vendors)]
            a = v["bank_accounts"][0]
            changed = i % 5 == 0                               # 1 in 5 pays a new account
            body = {"external_ref": f"BENCH:{tid}:{i}", "vendor": {"id": v["id"], "name": v["legal_name"], "country": v["country"]},
                    "amount": 1000 + (i * 37) % 9000, "currency": v["default_currency"],
                    "bank": {"country": a["country"], "account": f"9{i:011d}" if changed else a.get("account_number"),
                             "iban": None if changed else a.get("iban"), "swift": a.get("swift_bic"), "bank_name": a["bank_name"]}}
            async with sem:
                t0 = time.perf_counter()
                r = await c.post(f"/api/t/{tid}/erp/payments?wait=60", headers=auth, json=body)
                dt = time.perf_counter() - t0
            if r.status_code != 200 or not r.json().get("decision"):
                errors.append(f"{r.status_code} {r.text[:120]}")
                return
            lat.append(dt)
            d = r.json()["decision"]
            decisions[d] = decisions.get(d, 0) + 1

        await one(-1)                                          # warm caches (FX, GLEIF, sanctions lists)
        lat.clear(), decisions.clear()
        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(args.n)))
        wall = time.perf_counter() - t0
        audit = (await c.get(f"/api/t/{tid}/audit/verify", headers=owner)).json()
    ok = len(lat)
    print(f"{ok}/{args.n} payment checks completed in {wall:.1f}s with {args.concurrency} in flight "
          f"-> {ok / wall:.1f} checks/s")
    if lat:
        print(f"latency  p50 {pct(lat, 50) * 1000:.0f} ms   p95 {pct(lat, 95) * 1000:.0f} ms   "
              f"p99 {pct(lat, 99) * 1000:.0f} ms   mean {statistics.mean(lat) * 1000:.0f} ms")
    print(f"decisions {decisions}")
    print(f"audit chain after load: {'intact' if audit.get('ok') else 'BROKEN'} ({audit.get('entries')} entries)")
    if errors:
        print(f"{len(errors)} errors, first: {errors[0]}")


if __name__ == "__main__":
    asyncio.run(main())
