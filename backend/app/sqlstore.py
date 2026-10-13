"""SQL backend (PostgreSQL in production, SQLite for tests) so the product runs on any
cloud or on-premises: AWS RDS/Aurora, Azure Database for PostgreSQL, Cloud SQL, or a local DB.
Documents are stored as JSON rows keyed by path, mirroring the Firestore layout."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import JSON, Column, MetaData, String, Table, and_, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .blobs import Blobs
from .store import Store

meta = MetaData()
docs = Table("vbw_docs", meta,
             Column("path", String(512), primary_key=True),
             Column("coll", String(512), nullable=False, index=True),
             Column("data", JSON, nullable=False))

NUMERIC_FIELDS = {"seq", "version", "amount", "n"}
OPS = {"==": "__eq__", ">": "__gt__", "<": "__lt__", ">=": "__ge__", "<=": "__le__"}


def _coll(path: str) -> str:
    return path.rsplit("/", 1)[0]


def _clean(d: dict) -> dict:
    return json.loads(json.dumps(d, default=str))


def _field(name: str, sample: Any):
    expr = docs.c.data[name]
    if isinstance(sample, bool):
        return expr.as_boolean()
    if isinstance(sample, int) or (sample is None and name in NUMERIC_FIELDS):
        return expr.as_integer()
    if isinstance(sample, float):
        return expr.as_float()
    return expr.as_string()


class SqlStore(Store):
    def __init__(self, url: str, blobs: Blobs, engine: AsyncEngine | None = None):
        if engine is not None:
            self.engine = engine
        elif url.startswith("sqlite"):
            self.engine = create_async_engine(url)
        else:
            self.engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
        self.blobs = blobs
        self._ready = False

    async def _init(self):
        if not self._ready:
            async with self.engine.begin() as c:
                await c.run_sync(meta.create_all)
            self._ready = True

    async def get(self, path):
        await self._init()
        async with self.engine.connect() as c:
            row = (await c.execute(select(docs.c.data).where(docs.c.path == path))).first()
        return row[0] if row else None

    async def _upsert(self, c, path, data):
        r = await c.execute(update(docs).where(docs.c.path == path).values(data=data))
        if r.rowcount == 0:
            await c.execute(insert(docs).values(path=path, coll=_coll(path), data=data))

    async def set(self, path, data):
        await self._init()
        async with self.engine.begin() as c:
            await self._upsert(c, path, _clean(data))

    async def batch_set(self, items):
        await self._init()
        async with self.engine.begin() as c:
            for p, d in items:
                await self._upsert(c, p, _clean(d))

    async def delete(self, path):
        await self._init()
        async with self.engine.begin() as c:
            await c.execute(delete(docs).where(docs.c.path == path))

    async def query(self, coll, where=None, order_by=None, desc=False, limit=None):
        await self._init()
        conds = [docs.c.coll == coll]
        for f, op, v in where or []:
            conds.append(getattr(_field(f, v), OPS[op])(v))
        q = select(docs.c.data).where(and_(*conds))
        if order_by:
            col = _field(order_by, None)
            q = q.order_by(col.desc() if desc else col.asc())
        if limit:
            q = q.limit(limit)
        async with self.engine.connect() as c:
            return [r[0] for r in (await c.execute(q)).all()]

    async def atomic(self, path, fn):
        await self._init()
        async with self.engine.begin() as c:   # row lock on PostgreSQL; SQLite serialises writers
            row = (await c.execute(select(docs.c.data).where(docs.c.path == path).with_for_update())).first()
            new, res = fn(row[0] if row else None)
            if new is not None:
                await self._upsert(c, path, _clean(new))
            return res

    async def purge_for_tests(self):
        await self._init()
        async with self.engine.begin() as c:
            await c.execute(delete(docs))

    async def put_blob(self, key, data, mime):
        return await self.blobs.put(key, data, mime)

    async def get_blob(self, uri):
        return await self.blobs.get(uri)
