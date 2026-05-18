"""asyncpg connection pool for the agent service.

A single pool talks to ``pgbouncer:6432`` and carries everything the
service does against Postgres — agent tool reads plus trace writes.
Transaction-pooling mode means prepared-statement caching has to be off
(``statement_cache_size=0``); otherwise the cache and pgbouncer fight
over which server connection a statement belongs to.

There was previously a second, direct-to-Postgres read-only pool for a
``load_dataset_sql`` escape-hatch tool. That tool was removed (it ran
model-authored SQL and bypassed pgbouncer — a connection-scaling
liability); see DECISIONS.md. One pool, one path, through the pooler.
"""

from __future__ import annotations

import os
from typing import Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


async def init_pools() -> None:
    """Initialise the connection pool. Idempotent — safe to call from FastAPI startup."""
    global _pool

    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ["POSTGRES_PORT"]),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ["POSTGRES_DB"],
            min_size=2,
            max_size=10,
            statement_cache_size=0,
        )


async def close_pools() -> None:
    """Close the pool on FastAPI shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("connection pool not initialised; call init_pools() first")
    return _pool
