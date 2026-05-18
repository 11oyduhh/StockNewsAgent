"""Agent loop: LiteLLM tool-use cycle with compaction.

Drives one task to completion (or to the round cap):

  1. Seed messages = [system, user_task].
  2. For each round:
     a. Compact if cumulative input tokens crossed the threshold.
     b. ``litellm.acompletion`` with ``tools=TOOL_DEFINITIONS`` and
        ``metadata={task_id, turn_index}`` so the telemetry callback
        can stamp the right ``traces`` row.
     c. Append the assistant message.
     d. If no ``tool_calls``: done — return the assistant text.
     e. Otherwise dispatch each tool, write a ``tool_call`` trace row,
        and append the result as a ``role='tool'`` message.

If the loop runs out of rounds with tool calls still pending, the model
never got a turn to consume the last tool result. A final tool-free
``acompletion`` call then lets it synthesize an answer from the history.

``max_rounds_with_tool_calls`` is therefore the tool-use round budget,
not a hard cap on LLM calls — a capped run makes one extra synthesis
call (worst case: budget + 1 calls).

Each run owns a :class:`~service.datasets.DatasetRegistry` — heavy tools
load query results into it server-side and the agent works on handles, so
bulk rows never enter the context. The registry is cleared when the run ends.

Returns an :class:`AgentResult` with the answer plus a small run summary
(turns, tokens, cost, compactions, whether the round cap fired).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import litellm

from . import compaction, datasets, db, tools
from .prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Per-tool-call timeout. One hung tool shouldn't be able to stall a whole run.
# `asyncio.wait_for` bounds awaitable work — the DB round-trips, which are where
# a real hang (a lock, a pathological query) would happen. The dataset_* tools'
# in-memory pandas work is CPU-bound and not interruptible mid-call, but it is
# already bounded by the loader's row cap. 30s is generous: only a genuine hang
# trips it.
_TOOL_TIMEOUT_SECONDS = 30


@dataclass
class AgentResult:
    answer: str
    task_id: str
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    compactions: int
    hit_round_cap: bool


def _config_from_env() -> compaction.CompactionConfig:
    return compaction.CompactionConfig(
        auto_trigger_input_tokens=int(os.environ.get("COMPACTION_TRIGGER_TOKENS", "20000")),
        preserve_recent_messages=int(os.environ.get("COMPACTION_PRESERVE_RECENT", "4")),
    )


# Anthropic extended thinking — always on. It surfaces the model's reasoning,
# which the telemetry callback captures into traces and GET /traces/{task_id}
# exposes, so a run is auditable for *why* the agent chose each tool. Not an
# operational knob: it's a core observability decision for this platform.
#
# The interleaved-thinking beta makes the model reason before every tool call,
# not just the first turn. Anthropic requires max_tokens > budget_tokens; the
# +6144 leaves headroom for the answer itself.
_THINKING_BUDGET_TOKENS = 10_000

THINKING_KWARGS: dict[str, Any] = {
    "thinking": {"type": "enabled", "budget_tokens": _THINKING_BUDGET_TOKENS},
    "max_tokens": _THINKING_BUDGET_TOKENS + 6144,
    "extra_headers": {"anthropic-beta": "interleaved-thinking-2025-05-14"},
}


def _response_cost(response: Any) -> float:
    """Pull a $ cost off a LiteLLM response, tolerating a few API shapes."""
    try:
        from litellm import completion_cost

        return float(completion_cost(completion_response=response) or 0.0)
    except Exception:
        pass
    hidden = getattr(response, "_hidden_params", None) or {}
    for key in ("response_cost", "_response_cost"):
        v = hidden.get(key) if isinstance(hidden, dict) else None
        if v is not None:
            return float(v)
    return 0.0


async def run_agent(task: str, max_rounds_with_tool_calls: int | None = None) -> AgentResult:
    task_id = str(uuid4())
    model = os.environ.get("LITELLM_MODEL", "anthropic/claude-sonnet-4-6")
    if max_rounds_with_tool_calls is None:
        max_rounds_with_tool_calls = int(os.environ.get("AGENT_MAX_ROUNDS_WITH_TOOL_CALLS", "10"))
    # Guard the degenerate 0 / negative case — the env var is unvalidated.
    max_rounds_with_tool_calls = max(1, max_rounds_with_tool_calls)
    cfg = _config_from_env()

    # Per-run dataset store — heavy tools load into it, analysis tools read it,
    # and it is cleared in the finally below so memory is bounded to the run.
    registry = datasets.DatasetRegistry()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    cumulative_input = 0
    total_output = 0
    total_cost = 0.0
    compactions = 0
    final_text = ""

    try:
        for turn in range(max_rounds_with_tool_calls):
            # ── Maybe compact before this call ────────────────────────
            if compaction.should_compact(messages, cumulative_input, cfg):
                result = compaction.compact_messages(messages, cfg)
                if result.removed_count > 0:
                    messages = result.messages
                    compactions += 1
                    logger.info(
                        "task=%s turn=%d compacted removed=%d saved=%d walk_back=%d",
                        task_id,
                        turn,
                        result.removed_count,
                        result.estimated_tokens_saved,
                        result.walk_back_steps,
                    )

            # ── LLM call ──────────────────────────────────────────────
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                tools=tools.TOOL_DEFINITIONS,
                tool_choice="auto",
                metadata={"task_id": task_id, "turn_index": turn},
                **THINKING_KWARGS,
            )

            usage = getattr(response, "usage", None)
            if usage is not None:
                cumulative_input += int(getattr(usage, "prompt_tokens", 0) or 0)
                total_output += int(getattr(usage, "completion_tokens", 0) or 0)
            total_cost += _response_cost(response)

            msg = response.choices[0].message

            # Build the assistant message dict to append to history.
            assistant_dict: dict[str, Any] = {"role": "assistant"}
            if msg.content is not None:
                assistant_dict["content"] = msg.content
            # Extended thinking: the thinking blocks from a turn that produced
            # tool calls MUST be carried back with the assistant message, or
            # Anthropic rejects the next request. Preserve them verbatim.
            thinking_blocks = getattr(msg, "thinking_blocks", None)
            if thinking_blocks:
                assistant_dict["thinking_blocks"] = thinking_blocks
            if getattr(msg, "tool_calls", None):
                assistant_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_dict)

            # ── Done if no tool calls ─────────────────────────────────
            if not getattr(msg, "tool_calls", None):
                final_text = msg.content or ""
                return AgentResult(
                    answer=final_text,
                    task_id=task_id,
                    turns=turn + 1,
                    input_tokens=cumulative_input,
                    output_tokens=total_output,
                    cost_usd=total_cost,
                    compactions=compactions,
                    hit_round_cap=False,
                )

            # ── Dispatch each tool call, persist trace, append result ─
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                t0 = time.time()
                try:
                    tool_result = await asyncio.wait_for(
                        tools.dispatch(name, args, registry),
                        timeout=_TOOL_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Surface it as a normal tool error so the model can recover
                    # (try a narrower query) instead of the run hanging.
                    tool_result = {
                        "error": f"tool '{name}' exceeded the "
                        f"{_TOOL_TIMEOUT_SECONDS}s time limit and was cancelled"
                    }
                latency_ms = int((time.time() - t0) * 1000)

                try:
                    await db.pool().execute(
                        """
                        INSERT INTO traces
                            (task_id, turn_index, kind, model,
                             tool_name, tool_input, tool_output, latency_ms, error)
                        VALUES ($1, $2, 'tool_call', $3, $4,
                                $5::jsonb, $6::jsonb, $7, $8)
                        """,
                        UUID(task_id),
                        turn,
                        model,
                        name,
                        json.dumps(args, default=str),
                        json.dumps(tool_result, default=str)[:1_000_000],  # safety cap
                        latency_ms,
                        tool_result.get("error") if isinstance(tool_result, dict) else None,
                    )
                except Exception as exc:
                    # Never fail the agent loop on a trace write.
                    logger.warning("tool_call trace write failed: %s", exc)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": tools.tool_result_text(tool_result),
                    }
                )

        # Round budget exhausted with tool results still pending — the model
        # never got a turn to consume the last one. One tool-free call lets it
        # synthesize a final answer from the conversation history it already
        # has. ``tools`` is omitted entirely so the response can only be text.
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            metadata={"task_id": task_id, "turn_index": max_rounds_with_tool_calls},
            **THINKING_KWARGS,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            cumulative_input += int(getattr(usage, "prompt_tokens", 0) or 0)
            total_output += int(getattr(usage, "completion_tokens", 0) or 0)
        total_cost += _response_cost(response)
        final_text = response.choices[0].message.content or final_text

        return AgentResult(
            answer=final_text or "(no answer produced)",
            task_id=task_id,
            turns=max_rounds_with_tool_calls,
            input_tokens=cumulative_input,
            output_tokens=total_output,
            cost_usd=total_cost,
            compactions=compactions,
            hit_round_cap=True,
        )
    finally:
        # Per-run lifecycle: drop all loaded datasets when the run ends.
        logger.info("task=%s done — clearing %d dataset(s)", task_id, registry.count)
        registry.clear()
