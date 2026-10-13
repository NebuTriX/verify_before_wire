"""Tamper-evident audit log. Each entry stores the hash of the previous entry, so editing
or deleting any past entry breaks every hash after it. verify_chain() proves integrity."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .models import utcnow

GENESIS = "0" * 64


class AuditEntry(BaseModel):
    seq: int
    at: datetime = Field(default_factory=utcnow)
    actor: str                  # uid, "apikey:<id>" or "system"
    action: str                 # e.g. case.decided, policy.approved, member.added
    target: str                 # e.g. case/abc123
    detail: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str = ""


def entry_hash(e: AuditEntry) -> str:
    body = e.model_dump(mode="json", exclude={"hash"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seal(seq: int, prev_hash: str, actor: str, action: str, target: str, detail: dict) -> AuditEntry:
    e = AuditEntry(seq=seq, actor=actor, action=action, target=target, detail=detail, prev_hash=prev_hash)
    e.hash = entry_hash(e)
    return e


def verify_chain(entries: list[AuditEntry]) -> dict:
    prev = GENESIS
    for i, e in enumerate(sorted(entries, key=lambda x: x.seq)):
        if e.seq != i + 1:
            return {"ok": False, "broken_at": e.seq, "reason": "missing entry before this one"}
        if e.prev_hash != prev:
            return {"ok": False, "broken_at": e.seq, "reason": "previous-hash link does not match"}
        if entry_hash(e) != e.hash:
            return {"ok": False, "broken_at": e.seq, "reason": "entry content was changed"}
        prev = e.hash
    return {"ok": True, "entries": len(entries), "head": prev}
