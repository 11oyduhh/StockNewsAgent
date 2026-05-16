"""asyncpg pools for the agent service.

Two pools, intentionally separate:

* **writer** — talks to ``pgbouncer:6432``. Carries the bulk of agent
  tool reads plus all trace writes. Transaction-pooling mode means
  prepared-statement caching has to be off (``statement_cache_size=0``);
  otherwise the cache and pgbouncer fight over what server connection
  a statement belongs to.

* **reader** — talks to ``postgres:5432`` directly using the
  ``anthelion_ro`` role. Used **only** by the ``sql()`` escape-hatch
  tool. We bypass pgbouncer here because (a) the traffic is low and (b)
  we want a hard-enforced server-side ``statement_timeout`` per
  connection that doesn't interact with pooled connection reuse.
"""

from __future__ import annotations

import os
from typing import Optional

import asyncpg

_writer_pool: Optional[asyncpg.Pool] = None
_reader_pool: Optional[asyncpg.Pool] = None


async def init_pools() -> None:
    """Initialise both pools. Idempotent — safe to call from FastAPI startup."""
    global _writer_pool, _reader_pool

    if _writer_pool is None:
        _writer_pool = await asyncpg.create_pool(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ["POSTGRES_PORT"]),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ["POSTGRES_DB"],
            min_size=2,
            max_size=10,
            statement_cache_size=0,
        )

    if _reader_pool is None:
        timeout_ms = int(os.environ.get("AGENT_SQL_STATEMENT_TIMEOUT_MS", "10000"))

        async def _init_reader(conn: asyncpg.Connection) -> None:
            await conn.execute(f"SET statement_timeout = {timeout_ms}")
            # Belt-and-braces: the role already lacks write perms, but pinning
            # the session to read-only at the protocol level adds a second
            # ceiling that survives any future GRANT mistake.
            await conn.execute("SET default_transaction_read_only = on")

        _reader_pool = await asyncpg.create_pool(
            host=os.environ["POSTGRES_READONLY_HOST"],
            port=int(os.environ["POSTGRES_READONLY_PORT"]),
            user=os.environ["POSTGRES_READONLY_USER"],
            password=os.environ["POSTGRES_READONLY_PASSWORD"],
            database=os.environ["POSTGRES_DB"],
            min_size=1,
            max_size=3,
            init=_init_reader,
        )


async def close_pools() -> None:
    """Close both pools on FastAPI shutdown."""
    global _writer_pool, _reader_pool
    if _writer_pool is not None:
        await _writer_pool.close()
        _writer_pool = None
    if _reader_pool is not None:
        await _reader_pool.close()
        _reader_pool = None


def writer() -> asyncpg.Pool:
    if _writer_pool is None:
        raise RuntimeError("writer pool not initialised; call init_pools() first")
    return _writer_pool


def reader() -> asyncpg.Pool:
    if _reader_pool is None:
        raise RuntimeError("reader pool not initialised; call init_pools() first")
    return _reader_pool
