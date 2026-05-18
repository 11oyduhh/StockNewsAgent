"""FastAPI surface for the agent.

* ``POST /run`` — execute one task end-to-end; returns the answer + a
  small summary (turns, tokens, cost, whether the round cap fired).
* ``GET /healthz`` — liveness probe used by the compose healthcheck.
* ``GET /traces/{task_id}`` — replay one task's telemetry (every LLM
  call and tool call, in order, with the model's reasoning).

Lifespan wires ``litellm.callbacks`` to the Postgres trace logger so
every LLM call writes a ``traces`` row, and warms the asyncpg pool.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional
from uuid import UUID

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


# ── Telemetry surface ──────────────────────────────────────────────────
#
# Every run already writes to the `traces` table. This endpoint makes that
# data observable without a psql session: it replays one task's telemetry —
# every LLM call and tool call, in order, with the model's reasoning.


def _loads(value: Any) -> Any:
    """asyncpg hands JSONB back as a string; decode it (tolerate non-JSON)."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _reasoning_of(response: Any) -> Optional[str]:
    """Extended-thinking text from a captured LLM response, if any."""
    msg = _loads(response)
    if isinstance(msg, dict):
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
    return None


def _trace_step(row: Any, verbose: bool) -> dict:
    """One `traces` row → an ordered-timeline step."""
    kind = row["kind"]
    step: dict = {
        "turn": row["turn_index"],
        "kind": kind,
        "ts": row["ts"].isoformat() if row["ts"] else None,
        "latency_ms": row["latency_ms"],
        "error": row["error"],
    }
    if kind == "llm_call":
        step["model"] = row["model"]
        step["input_tokens"] = row["input_tokens"]
        step["output_tokens"] = row["output_tokens"]
        step["cost_usd"] = float(row["cost_usd"]) if row["cost_usd"] is not None else None
        # The telemetry writer stows the requested-tool-name list in tool_input.
        step["tool_calls"] = _loads(row["tool_input"])
        step["reasoning"] = _reasoning_of(row["response"])
        if verbose:
            step["prompt"] = _loads(row["prompt"])
            step["response"] = _loads(row["response"])
    else:  # tool_call
        step["tool_name"] = row["tool_name"]
        if verbose:
            step["tool_input"] = _loads(row["tool_input"])
            step["tool_output"] = _loads(row["tool_output"])
    return step


@app.get("/traces/{task_id}")
async def traces(task_id: str, verbose: bool = False) -> dict:
    """Replay one task's telemetry — every LLM call and tool call, in order.

    Default: per-step metadata + the model's extended-thinking reasoning.
    `?verbose=true` also returns full prompts, responses, and tool I/O.
    """
    try:
        tid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="task_id must be a UUID")
    rows = await db.pool().fetch(
        """
        SELECT turn_index, kind, model, tool_name,
               input_tokens, output_tokens, cost_usd, latency_ms, error,
               ts, prompt, response, tool_input, tool_output
        FROM traces
        WHERE task_id = $1
        ORDER BY turn_index, (kind <> 'llm_call'), id
        """,
        tid,
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"no traces for task {task_id}")
    return {
        "task_id": task_id,
        "step_count": len(rows),
        "steps": [_trace_step(r, verbose) for r in rows],
    }
