"""Per-workspace token bucket for new checks. Counted per process: behind several replicas, add a
limit at the gateway as well (see docs/production.md)."""
from __future__ import annotations

import time

from fastapi import HTTPException

_buckets: dict[str, tuple[float, float]] = {}      # tenant id -> (tokens left, last refill time)


def charge(tid: str, per_min: int) -> None:
    """Take one token for this workspace or raise 429."""
    now = time.monotonic()
    tokens, last = _buckets.get(tid, (per_min, now))
    tokens = min(per_min, tokens + (now - last) * per_min / 60)
    if tokens < 1:
        raise HTTPException(429, "Too many checks in the last minute. Try again shortly.")
    _buckets[tid] = (tokens - 1, now)
