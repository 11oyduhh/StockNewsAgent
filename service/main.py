"""FastAPI surface for the agent.

* ``POST /run`` — execute one task end-to-end; returns the answer + a
  small summary (turns, tokens, cost, whether the round cap fired).
* ``GET /healthz`` — liveness probe used by the compose healthcheck.

Lifespan wires ``litellm.callbacks`` to the Postgres trace logger so
every LLM call writes a ``traces`` row, and warms both asyncpg pools.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

import litellm
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db, loop
from .telemetry import PostgresTraceLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pools()
    litellm.callbacks = [PostgresTraceLogger()]
    logger.info("agent service ready — pools warmed, telemetry wired")
    try:
        yield
    finally:
        litellm.callbacks = []
        await db.close_pools()


app = FastAPI(title="anthelion-agent", lifespan=lifespan)


class RunRequest(BaseModel):
    task: str = Field(..., description="Natural-language task for the agent.")
    max_rounds_with_tool_calls: Optional[int] = Field(None, ge=1, le=50)


class RunResponse(BaseModel):
    task_id: str
    answer: str
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    compactions: int
    hit_round_cap: bool


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    task = (req.task or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="task must be non-empty")
    try:
        result = await loop.run_agent(
            task, max_rounds_with_tool_calls=req.max_rounds_with_tool_calls
        )
    except Exception as exc:
        logger.exception("agent loop failed")
        raise HTTPException(status_code=500, detail=f"agent loop failed: {exc}") from exc
    return RunResponse(**result.__dict__)
