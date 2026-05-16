"""LiteLLM completion events → Postgres ``traces`` rows.

Subclass of :class:`litellm.integrations.custom_logger.CustomLogger` that
forwards every LLM call (success or failure) into one trace row. Captures
model, prompt / response shapes, token counts, cost, latency, tool-call
names, error string.

Adapted from the AgenticCRE ``litellm_telemetry.py`` pattern but with a
single sink (the ``traces`` table) instead of a JSONL recorder bridge.
Per-call ``metadata`` lets the caller stamp the ``task_id`` and
``turn_index`` so we can group rows by run.

Defensive: a failure in telemetry never breaks the LLM call path — the
write is wrapped in try/except, errors only logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from litellm.integrations.custom_logger import CustomLogger

from . import db

logger = logging.getLogger(__name__)


class PostgresTraceLogger(CustomLogger):
    """LiteLLM callback that writes one ``traces`` row per LLM call."""

    # ── Sync hooks (LiteLLM picks based on whether the call site is async)

    def log_success_event(self, kwargs: dict, response_obj: Any, start_time, end_time) -> None:
        self._schedule(kwargs, response_obj, start_time, end_time, error=None)

    def log_failure_event(self, kwargs: dict, response_obj: Any, start_time, end_time) -> None:
        err = str(kwargs.get("exception") or response_obj or "unknown error")
        self._schedule(kwargs, response_obj, start_time, end_time, error=err)

    async def async_log_success_event(
        self, kwargs: dict, response_obj: Any, start_time, end_time
    ) -> None:
        await self._write(kwargs, response_obj, start_time, end_time, error=None)

    async def async_log_failure_event(
        self, kwargs: dict, response_obj: Any, start_time, end_time
    ) -> None:
        err = str(kwargs.get("exception") or response_obj or "unknown error")
        await self._write(kwargs, response_obj, start_time, end_time, error=err)

    # ── Internals ──────────────────────────────────────────────────────

    def _schedule(
        self, kwargs, response_obj, start_time, end_time, *, error: Optional[str]
    ) -> None:
        """Fire-and-forget the async write from a sync hook."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            asyncio.create_task(
                self._write(kwargs, response_obj, start_time, end_time, error=error)
            )
        else:
            # No running loop — synchronously run the write to completion.
            asyncio.run(self._write(kwargs, response_obj, start_time, end_time, error=error))

    async def _write(
        self, kwargs, response_obj, start_time, end_time, *, error: Optional[str]
    ) -> None:
        try:
            metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
            task_id_str = metadata.get("task_id")
            turn_index = metadata.get("turn_index")
            if not task_id_str or turn_index is None:
                # Caller didn't stamp a task — skip silently. (Avoids writing
                # orphan rows for any incidental LiteLLM call not in our loop.)
                return

            task_id = UUID(task_id_str) if isinstance(task_id_str, str) else task_id_str
            model = kwargs.get("model")
            prompt = _safe_json(kwargs.get("messages"))
            response = _safe_json(_extract_response_message(response_obj))
            input_tokens = _usage_field(response_obj, "prompt_tokens")
            output_tokens = _usage_field(response_obj, "completion_tokens")
            cost = kwargs.get("response_cost")
            latency_ms = _latency_ms(start_time, end_time)
            tool_names = _tool_call_summary(response_obj)
            tool_calls_json = json.dumps(tool_names) if tool_names else None

            await db.writer().execute(
                """
                INSERT INTO traces
                    (task_id, turn_index, kind, model, prompt, response,
                     tool_name, tool_input, tool_output,
                     input_tokens, output_tokens, cost_usd, latency_ms, error)
                VALUES ($1, $2, 'llm_call', $3, $4::jsonb, $5::jsonb,
                        NULL, $6::jsonb, NULL,
                        $7, $8, $9, $10, $11)
                """,
                task_id,
                int(turn_index),
                model,
                prompt,
                response,
                tool_calls_json,
                input_tokens,
                output_tokens,
                cost,
                latency_ms,
                error,
            )
        except Exception as exc:
            # Never let a telemetry write break the LLM call path.
            logger.warning("PostgresTraceLogger write failed: %s", exc)


# ── Helpers ────────────────────────────────────────────────────────────


def _safe_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except Exception:
        return json.dumps(str(value))


def _extract_response_message(response_obj: Any) -> Any:
    """Pull the assistant message dict out of a LiteLLM completion response."""
    try:
        choices = getattr(response_obj, "choices", None) or []
        if not choices:
            return None
        message = getattr(choices[0], "message", None)
        if message is None:
            return None
        # ModelResponse.message is a Pydantic-like object; .model_dump() or
        # .__dict__ both work in practice. Fall back to repr-ish dict.
        if hasattr(message, "model_dump"):
            return message.model_dump()
        if hasattr(message, "__dict__"):
            return {k: v for k, v in message.__dict__.items() if not k.startswith("_")}
        return str(message)
    except Exception:
        return None


def _usage_field(response_obj: Any, name: str) -> Optional[int]:
    usage = getattr(response_obj, "usage", None)
    if usage is None:
        return None
    value = getattr(usage, name, None)
    return int(value) if value is not None else None


def _tool_call_summary(response_obj: Any) -> Optional[list[str]]:
    """List of tool names invoked in the response, or None."""
    try:
        choices = getattr(response_obj, "choices", None) or []
        if not choices:
            return None
        message = getattr(choices[0], "message", None)
        if message is None:
            return None
        tool_calls = getattr(message, "tool_calls", None) or []
        names = [getattr(tc.function, "name", "?") for tc in tool_calls]
        return names or None
    except Exception:
        return None


def _latency_ms(start_time: Any, end_time: Any) -> Optional[int]:
    try:
        if isinstance(start_time, datetime) and isinstance(end_time, datetime):
            return int((end_time - start_time).total_seconds() * 1000)
        delta = end_time - start_time
        seconds = getattr(delta, "total_seconds", lambda: float(delta))()
        return int(seconds * 1000)
    except Exception:
        return None
