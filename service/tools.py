"""Tool registry for the agent.

Eight tools plus a dispatcher. Each tool is an async function returning
a JSON-serialisable dict; ``TOOL_DEFINITIONS`` is the OpenAI-format
schema list passed to LiteLLM; ``dispatch(name, args)`` runs the named
tool and returns its result (or a structured error).

The ``sql`` tool routes through the read-only pool, which has
``statement_timeout`` and ``default_transaction_read_only`` set per
connection. Everything else uses the writer pool.

Conventions:

* Dates accepted as ISO ``YYYY-MM-DD`` strings.
* Result rows: dates rendered as ISO strings, NUMERIC as floats. The
  ``_json_safe`` helper normalises asyncpg Record / Decimal / date.
* Empty result sets return ``{"rows": [], "note": "<hint>"}`` rather
  than erroring — lets the agent decide whether to broaden the query.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable

import asyncpg

from . import db

logger = logging.getLogger(__name__)


# ── JSON serialisation ─────────────────────────────────────────────────


def _json_safe(o: Any) -> Any:
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, asyncpg.Record):
        return {k: _json_safe(v) for k, v in dict(o).items()}
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return str(o)


# ── 1. headline_search ─────────────────────────────────────────────────


async def headline_search(
    query: str,
    source: str = "all",
    ticker: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> dict:
    """FTS over headlines with optional filters.

    ``source``: ``'abc' | 'analyst' | 'partner' | 'all'``.
    Returns highest-ts_rank rows up to ``limit`` (max 100).
    """
    limit = max(1, min(int(limit), 100))
    source = (source or "all").lower()
    pool = db.writer()

    results: list[dict] = []

    if source in ("abc", "all") and not ticker:
        # ABC has no ticker column; skip when caller filters by ticker.
        rows = await pool.fetch(
            """
            SELECT 'abc' AS source,
                   publish_date::text AS date,
                   headline,
                   NULL::text AS ticker,
                   NULL::text AS publisher,
                   ts_rank(headline_tsv, q) AS rank
            FROM abc_headlines, websearch_to_tsquery('english', $1) q
            WHERE headline_tsv @@ q
              AND ($2::date IS NULL OR publish_date >= $2::date)
              AND ($3::date IS NULL OR publish_date <= $3::date)
            ORDER BY rank DESC
            LIMIT $4
            """,
            query,
            start_date,
            end_date,
            limit,
        )
        results.extend(_json_safe(rows))

    if source in ("analyst", "partner", "all"):
        src_filter = None if source == "all" else source
        rows = await pool.fetch(
            """
            SELECT source,
                   published_at::date::text AS date,
                   headline,
                   ticker,
                   publisher,
                   ts_rank(headline_tsv, q) AS rank
            FROM us_headlines, websearch_to_tsquery('english', $1) q
            WHERE headline_tsv @@ q
              AND ($2::text IS NULL OR source = $2)
              AND ($3::text IS NULL OR ticker = $3)
              AND ($4::date IS NULL OR published_at >= $4::date)
              AND ($5::date IS NULL OR published_at <= ($5::date + INTERVAL '1 day'))
            ORDER BY rank DESC
            LIMIT $6
            """,
            query,
            src_filter,
            ticker,
            start_date,
            end_date,
            limit,
        )
        results.extend(_json_safe(rows))

    # Re-sort the merged list by rank when querying 'all'.
    results.sort(key=lambda r: r.get("rank") or 0, reverse=True)
    results = results[:limit]
    if not results:
        return {
            "rows": [],
            "note": "no matches — try broader keywords, a wider date range, or source='all'",
        }
    return {"rows": results, "count": len(results)}


# ── 2. headline_topic_frequency ────────────────────────────────────────


async def headline_topic_frequency(
    query: str,
    granularity: str = "month",
    source: str = "all",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Bucketed counts of FTS matches over time.

    ``granularity``: ``'day' | 'week' | 'month' | 'year'``.
    """
    if granularity not in ("day", "week", "month", "year"):
        return {"error": f"invalid granularity {granularity!r}; use day|week|month|year"}
    source = (source or "all").lower()

    parts: list[asyncpg.Record] = []
    pool = db.writer()

    if source in ("abc", "all"):
        rows = await pool.fetch(
            f"""
            SELECT date_trunc('{granularity}', publish_date)::date::text AS bucket,
                   COUNT(*) AS count,
                   'abc' AS source
            FROM abc_headlines, websearch_to_tsquery('english', $1) q
            WHERE headline_tsv @@ q
              AND ($2::date IS NULL OR publish_date >= $2::date)
              AND ($3::date IS NULL OR publish_date <= $3::date)
            GROUP BY bucket
            ORDER BY bucket
            """,
            query,
            start_date,
            end_date,
        )
        parts.extend(rows)

    if source in ("analyst", "partner", "all"):
        src_filter = None if source == "all" else source
        rows = await pool.fetch(
            f"""
            SELECT date_trunc('{granularity}', published_at)::date::text AS bucket,
                   COUNT(*) AS count,
                   COALESCE($2, 'us-all') AS source
            FROM us_headlines, websearch_to_tsquery('english', $1) q
            WHERE headline_tsv @@ q
              AND ($2::text IS NULL OR source = $2)
              AND ($3::date IS NULL OR published_at >= $3::date)
              AND ($4::date IS NULL OR published_at <= ($4::date + INTERVAL '1 day'))
            GROUP BY bucket
            ORDER BY bucket
            """,
            query,
            src_filter,
            start_date,
            end_date,
        )
        parts.extend(rows)

    if not parts:
        return {"rows": [], "note": "no matches in range"}
    return {"rows": _json_safe(parts), "granularity": granularity, "source": source}


# ── 3. lookup_security ─────────────────────────────────────────────────


async def lookup_security(name_or_ticker: str) -> dict:
    """Find securities by exact ticker or fuzzy name match (ILIKE)."""
    pool = db.writer()
    rows = await pool.fetch(
        """
        SELECT ticker, security_name, sector, sub_industry, address,
               date_first_added::text AS date_first_added, cik
        FROM securities
        WHERE ticker = upper($1)
           OR security_name ILIKE '%' || $1 || '%'
        ORDER BY (ticker = upper($1)) DESC, security_name
        LIMIT 10
        """,
        name_or_ticker,
    )
    if not rows:
        return {"rows": [], "note": f"no securities matched {name_or_ticker!r}"}
    return {"rows": _json_safe(rows), "count": len(rows)}


# ── 4. price_history ───────────────────────────────────────────────────


async def price_history(ticker: str, start_date: str, end_date: str) -> dict:
    """Daily OHLCV for a ticker over an inclusive date range."""
    pool = db.writer()
    rows = await pool.fetch(
        """
        SELECT date::text AS date, open, high, low, close, volume
        FROM prices
        WHERE ticker = upper($1) AND date BETWEEN $2::date AND $3::date
        ORDER BY date
        """,
        ticker,
        start_date,
        end_date,
    )
    if not rows:
        return {
            "rows": [],
            "note": f"no price rows for {ticker} in {start_date}..{end_date} (data ends 2016-12-30)",
        }
    return {"rows": _json_safe(rows), "count": len(rows), "ticker": ticker.upper()}


# ── 5. returns ─────────────────────────────────────────────────────────


async def returns(ticker: str, start_date: str, end_date: str) -> dict:
    """Daily close-to-close pct returns for a ticker."""
    pool = db.writer()
    rows = await pool.fetch(
        """
        WITH p AS (
            SELECT date, close,
                   LAG(close) OVER (ORDER BY date) AS prev_close
            FROM prices
            WHERE ticker = upper($1) AND date BETWEEN $2::date AND $3::date
        )
        SELECT date::text AS date,
               close,
               CASE WHEN prev_close IS NULL OR prev_close = 0 THEN NULL
                    ELSE (close - prev_close) / prev_close END AS return_pct
        FROM p
        WHERE prev_close IS NOT NULL
        ORDER BY date
        """,
        ticker,
        start_date,
        end_date,
    )
    if not rows:
        return {"rows": [], "note": f"no returns for {ticker} in range"}
    return {"rows": _json_safe(rows), "count": len(rows), "ticker": ticker.upper()}


# ── 6. forecast_returns (ARIMA) ────────────────────────────────────────


async def forecast_returns(ticker: str, horizon: int = 10) -> dict:
    """ARIMA(1,0,1) forecast of next-N daily returns.

    Fits on all available history for the ticker. Naive baseline — call
    out the limitations when surfacing to users.
    """
    horizon = max(1, min(int(horizon), 30))
    pool = db.writer()
    rows = await pool.fetch(
        """
        WITH p AS (
            SELECT date, close, LAG(close) OVER (ORDER BY date) AS prev_close
            FROM prices WHERE ticker = upper($1)
        )
        SELECT date::text AS date,
               (close - prev_close) / prev_close AS r
        FROM p
        WHERE prev_close IS NOT NULL AND prev_close <> 0
        ORDER BY date
        """,
        ticker,
    )
    if not rows:
        return {"error": f"no price history for {ticker}"}
    series = [float(r["r"]) for r in rows]
    if len(series) < 30:
        return {"error": f"insufficient history ({len(series)} rows) to fit ARIMA"}

    def _fit_and_forecast() -> dict:
        # Lazy import — statsmodels is heavy. Only the forecast tool needs it.
        from statsmodels.tsa.arima.model import ARIMA

        model = ARIMA(series, order=(1, 0, 1))
        fitted = model.fit()
        forecast = fitted.get_forecast(steps=horizon)
        mean = forecast.predicted_mean
        ci = forecast.conf_int(alpha=0.05)
        return {
            "mean": [float(x) for x in mean],
            "lower_95": [float(row[0]) for row in ci],
            "upper_95": [float(row[1]) for row in ci],
            "aic": float(fitted.aic),
        }

    try:
        fit = await asyncio.to_thread(_fit_and_forecast)
    except Exception as exc:
        return {"error": f"ARIMA fit failed: {exc}"}

    return {
        "ticker": ticker.upper(),
        "horizon": horizon,
        "history_rows": len(series),
        "last_observed_date": rows[-1]["date"].isoformat(),
        "forecast": [
            {
                "step": i + 1,
                "predicted_return": fit["mean"][i],
                "lower_95": fit["lower_95"][i],
                "upper_95": fit["upper_95"][i],
            }
            for i in range(horizon)
        ],
        "model": "ARIMA(1,0,1) on raw daily returns",
        "aic": fit["aic"],
        "caveat": "naive baseline; not a research-grade forecast",
    }


# ── 7. fundamentals_lookup ─────────────────────────────────────────────


async def fundamentals_lookup(ticker: str, metric: str, as_of: str | None = None) -> dict:
    """Pull a fundamental metric from the JSONB `data` column.

    Returns all available period-ends for the metric, or just the one
    matching ``as_of`` if supplied. Use the exact metric name as it
    appears in the source (e.g. ``"Total Revenue"``).
    """
    pool = db.writer()
    rows = await pool.fetch(
        """
        SELECT period_end::text AS period_end,
               for_year,
               data->>$2 AS value
        FROM fundamentals
        WHERE ticker = upper($1)
          AND ($3::date IS NULL OR period_end = $3::date)
          AND data ? $2
        ORDER BY period_end DESC
        """,
        ticker,
        metric,
        as_of,
    )
    if not rows:
        return {
            "rows": [],
            "note": f"no fundamentals for {ticker}/{metric!r}; check the metric name spelling",
        }
    return {"rows": _json_safe(rows), "ticker": ticker.upper(), "metric": metric}


# ── 8. sql (read-only escape hatch) ────────────────────────────────────


_SQL_PREFIXES = ("SELECT", "WITH")


async def sql(query: str) -> dict:
    """Run a read-only SQL query. SELECT/WITH only; row cap and timeout apply."""
    q = (query or "").strip().rstrip(";")
    if not q:
        return {"error": "empty query"}
    if not q.upper().lstrip("(").startswith(_SQL_PREFIXES):
        return {"error": "only SELECT or WITH queries are permitted via this tool"}
    cap = int(os.environ.get("AGENT_SQL_ROW_CAP", "1000"))
    try:
        async with db.reader().acquire() as conn:
            records = await conn.fetch(q)
    except Exception as exc:
        return {"error": str(exc)}
    truncated = len(records) > cap
    records = records[:cap]
    if not records:
        return {"columns": [], "rows": [], "truncated": False, "note": "0 rows"}
    columns = list(records[0].keys())
    rows = [[_json_safe(v) for v in r.values()] for r in records]
    return {"columns": columns, "rows": rows, "truncated": truncated, "row_count": len(rows)}


# ── Registry ───────────────────────────────────────────────────────────


TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "headline_search",
            "description": (
                "Full-text search over the headline corpora. Supports filtering by source "
                "(`abc`, `analyst`, `partner`, or `all`), by ticker (US sources only), and "
                "by date range. Ranked by ts_rank; returns at most 100 rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": 'Search expression. Supports websearch syntax ("phrase", OR, -exclude).',
                    },
                    "source": {
                        "type": "string",
                        "enum": ["abc", "analyst", "partner", "all"],
                        "default": "all",
                    },
                    "ticker": {
                        "type": "string",
                        "description": "Filter to a specific ticker (US sources only).",
                    },
                    "start_date": {"type": "string", "description": "ISO YYYY-MM-DD, inclusive."},
                    "end_date": {"type": "string", "description": "ISO YYYY-MM-DD, inclusive."},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "headline_topic_frequency",
            "description": "Count headlines matching an FTS query, bucketed by day/week/month/year.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "granularity": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                        "default": "month",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["abc", "analyst", "partner", "all"],
                        "default": "all",
                    },
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_security",
            "description": "Find S&P 500 securities by ticker symbol (exact) or company name (fuzzy ILIKE).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_or_ticker": {"type": "string"},
                },
                "required": ["name_or_ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "price_history",
            "description": "Daily OHLCV for a ticker, split-adjusted, between start_date and end_date (inclusive). Data ends 2016-12-30.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "start_date": {"type": "string", "description": "ISO YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "ISO YYYY-MM-DD"},
                },
                "required": ["ticker", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "returns",
            "description": "Daily close-to-close pct returns for a ticker over a date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                },
                "required": ["ticker", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forecast_returns",
            "description": (
                "ARIMA(1,0,1) forecast of next-N daily returns for a ticker. "
                "Fitted on all available history. Naive baseline — not a research-grade forecast."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "horizon": {"type": "integer", "default": 10, "minimum": 1, "maximum": 30},
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fundamentals_lookup",
            "description": (
                "Pull a specific fundamental metric for a ticker from the JSONB `data` column. "
                "Use the exact metric name as it appears in the source CSV "
                "(e.g. 'Total Revenue', 'Net Income', 'Earnings Per Share')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "metric": {"type": "string"},
                    "as_of": {
                        "type": "string",
                        "description": "Filter to a specific period_end (ISO YYYY-MM-DD). Optional.",
                    },
                },
                "required": ["ticker", "metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sql",
            "description": (
                "Read-only escape hatch for queries the structured tools don't cover. "
                "SELECT or WITH only. Row cap and statement timeout apply. "
                "Use sparingly — prefer the structured tools when one fits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A SELECT or WITH ... SELECT query.",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


_ToolFn = Callable[..., Awaitable[dict]]

_DISPATCH: dict[str, _ToolFn] = {
    "headline_search": headline_search,
    "headline_topic_frequency": headline_topic_frequency,
    "lookup_security": lookup_security,
    "price_history": price_history,
    "returns": returns,
    "forecast_returns": forecast_returns,
    "fundamentals_lookup": fundamentals_lookup,
    "sql": sql,
}


async def dispatch(name: str, args: dict) -> dict:
    """Run the named tool. Returns a JSON-serialisable dict (or an error dict)."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return await fn(**(args or {}))
    except TypeError as exc:
        # Bad argument shape — surface it back to the model so it can correct.
        return {"error": f"argument error: {exc}"}
    except Exception as exc:
        logger.exception("tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


def tool_result_text(result: Any) -> str:
    """Render a tool result for the tool message body (LiteLLM expects text)."""
    return json.dumps(_json_safe(result), default=str)
