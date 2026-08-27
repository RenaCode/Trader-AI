"""Pula polaczen do Postgresa. Jedno miejsce, wspolne dla wszystkich routerow."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

_pool: AsyncConnectionPool | None = None


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL nie jest ustawiony")
    return url


async def open_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(dsn(), min_size=1, max_size=8, open=False,
                                    kwargs={"row_factory": dict_row})
        await _pool.open(wait=True, timeout=15)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def conn():
    pool = await open_pool()
    async with pool.connection() as c:
        yield c


async def fetch_all(sql: str, params: tuple | dict | None = None) -> list[dict]:
    async with conn() as c:
        cur = await c.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(sql: str, params: tuple | dict | None = None) -> dict | None:
    async with conn() as c:
        cur = await c.execute(sql, params)
        return await cur.fetchone()
