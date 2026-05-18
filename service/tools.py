"""Tool registry for the agent.

Two kinds of tools:

* **Inline tools** return small results directly (``headline_search``,
  ``headline_topic_frequency``, ``lookup_security``).
* **Loaders** (``load_prices``) pull a potentially
  large result set into a server-side pandas DataFrame held in the run's
  :class:`~service.datasets.DatasetRegistry`, and return only a *handle* +
  shape/schema/head. **Analysis tools** (``dataset_*``) then operate on a
  handle and return small results.

The agent never receives bulk rows — see ``service/datasets.py`` and
DECISIONS.md. ``TOOL_DEFINITIONS`` is the OpenAI-format schema list passed to
LiteLLM; ``dispatch(name, args, registry)`` runs the named tool.

Conventions:

* Dates accepted as ISO ``YYYY-MM-DD`` strings.
* Empty / not-found results return ``{"error": "..."}`` or
  ``{"rows": [], "note": "..."}`` so the agent can recover.
* Analysis tools validate the handle's schema (column exists, dtype is
  numeric where required) and return a clear error on misapplication.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable

import asyncpg
import pandas as pd

from . import datasets, db
from .datasets import DatasetError, DatasetRegistry, scalar

logger = logging.getLogger(__name__)


# ── Serialisation helpers ──────────────────────────────────────────────


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


def _records_to_frame(rows: list[asyncpg.Record]) -> pd.DataFrame:
    """asyncpg records → DataFrame, coercing NUMERIC (Decimal) columns to float."""
    df = pd.DataFrame([dict(r) for r in rows])
    for col in df.columns:
        non_null = df[col].dropna()
        if not non_null.empty and isinstance(non_null.iloc[0], Decimal):
            df[col] = df[col].astype("float64")
    return df


# ── Inline tool 1: headline_search ─────────────────────────────────────


def _parse_optional_date(value: str | None) -> date | None:
    """ISO ``YYYY-MM-DD`` string → ``date`` (or ``None``).

    asyncpg binds ``date`` parameters as :class:`datetime.date`, not strings —
    the agent supplies ISO strings, so they must be parsed here. Raises
    ``ValueError`` on malformed input; callers surface it as a tool error.
    """
    return date.fromisoformat(value) if value else None


async def headline_search(
    query: str,
    source: str = "all",
    ticker: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 25,
) -> dict:
    """FTS over headlines with optional filters.

    ``source``: ``'abc' | 'analyst' | 'partner' | 'all'``. Returns the
    highest-ts_rank rows up to ``limit`` (max 50) — the agent reads these
    directly, so the cap is small on purpose.
    """
    limit = max(1, min(int(limit), 50))
    source = (source or "all").lower()
    try:
        start = _parse_optional_date(start_date)
        end = _parse_optional_date(end_date)
    except ValueError as exc:
        return {"error": f"bad date — use ISO YYYY-MM-DD ({exc})"}
    pool = db.pool()
    results: list[dict] = []

    if source in ("abc", "all") and not ticker:
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
            start,
            end,
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
            start,
            end,
            limit,
        )
        results.extend(_json_safe(rows))

    results.sort(key=lambda r: r.get("rank") or 0, reverse=True)
    results = results[:limit]
    if not results:
        return {
            "rows": [],
            "note": "no matches — try broader keywords, a wider date range, or source='all'",
        }
    return {"rows": results, "count": len(results)}


# ── Inline tool 2: headline_topic_frequency ────────────────────────────


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
    try:
        start = _parse_optional_date(start_date)
        end = _parse_optional_date(end_date)
    except ValueError as exc:
        return {"error": f"bad date — use ISO YYYY-MM-DD ({exc})"}
    parts: list[asyncpg.Record] = []
    pool = db.pool()

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
            start,
            end,
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
            start,
            end,
        )
        parts.extend(rows)

    if not parts:
        return {"rows": [], "note": "no matches in range"}
    result: dict[str, Any] = {
        "rows": _json_safe(parts),
        "granularity": granularity,
        "source": source,
        "count": len(parts),
    }
    if len(parts) > 120:
        result["hint"] = (
            "many buckets returned — use a coarser granularity "
            "(week/month/year) or a narrower date range"
        )
    return result


# ── Inline tool 3: lookup_security ─────────────────────────────────────


async def lookup_security(name_or_ticker: str) -> dict:
    """Find securities by exact ticker or fuzzy name match (ILIKE)."""
    pool = db.pool()
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


# ── Loader 1: load_prices ──────────────────────────────────────────────


async def load_prices(
    registry: DatasetRegistry, ticker: str, start_date: str, end_date: str
) -> dict:
    """Load daily OHLCV + a derived ``daily_return`` column into a dataset.

    Returns a handle + shape/schema/head — not the rows. Run the
    ``dataset_*`` analysis tools on the handle.
    """
    # asyncpg binds `date` params as datetime.date, not ISO strings — convert
    # the agent-supplied strings before they reach the driver.
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        return {"error": f"bad date — use ISO YYYY-MM-DD ({exc})"}
    pool = db.pool()
    rows = await pool.fetch(
        """
        WITH p AS (
            SELECT date, open, high, low, close, volume,
                   LAG(close) OVER (ORDER BY date) AS prev_close
            FROM prices
            WHERE ticker = upper($1) AND date BETWEEN $2 AND $3
        )
        SELECT date::text AS date, open, high, low, close, volume,
               CASE WHEN prev_close IS NULL OR prev_close = 0 THEN NULL
                    ELSE (close - prev_close) / prev_close END AS daily_return
        FROM p
        ORDER BY date
        """,
        ticker,
        start,
        end,
    )
    if not rows:
        return {
            "error": f"no price rows for {ticker} in {start_date}..{end_date} "
            "(price data covers 2010-01-04 .. 2016-12-30)"
        }
    df = _records_to_frame(rows)
    handle = registry.put(df)
    meta = registry.describe(handle)
    meta["loaded"] = f"daily prices for {ticker.upper()}, {start_date}..{end_date}"
    return meta


# ── Analysis: dataset_describe ─────────────────────────────────────────


async def dataset_describe(registry: DatasetRegistry, handle: str) -> dict:
    """Per-column summary statistics for a loaded dataset (pandas describe)."""
    try:
        df = registry.get(handle)
    except DatasetError as exc:
        return {"error": str(exc)}
    try:
        desc = df.describe(include="all")
    except Exception as exc:
        return {"error": f"could not describe dataset: {exc}"}
    stats: dict[str, dict] = {}
    for col in desc.columns:
        col_stats = {}
        for stat in desc.index:
            val = scalar(desc.loc[stat, col])
            if val is not None:
                col_stats[str(stat)] = val
        stats[str(col)] = col_stats
    return {"handle": handle, "n_rows": int(len(df)), "stats": stats}


# ── Analysis: dataset_sample ───────────────────────────────────────────


async def dataset_sample(
    registry: DatasetRegistry,
    handle: str,
    n: int = 10,
    sort_by: str | None = None,
    descending: bool = False,
) -> dict:
    """Return a small bounded slice of actual rows (e.g. the 5 worst days).

    ``n`` is capped at AGENT_DATASET_SAMPLE_MAX. Optionally sort by a column
    first — the only way raw rows reach the agent, always bounded.
    """
    try:
        df = registry.get(handle)
    except DatasetError as exc:
        return {"error": str(exc)}
    cap = int(os.environ.get("AGENT_DATASET_SAMPLE_MAX", "50"))
    n = max(1, min(int(n), cap))
    view = df
    if sort_by is not None:
        if sort_by not in df.columns:
            return {"error": f"column {sort_by!r} not in dataset; columns: {list(df.columns)}"}
        view = df.sort_values(sort_by, ascending=not descending)
    return {
        "handle": handle,
        "rows": datasets.frame_records(view.head(n)),
        "row_count": min(n, len(df)),
        "total_rows": int(len(df)),
        "sorted_by": sort_by,
        "descending": descending if sort_by else None,
    }


# ── Analysis: dataset_rolling ──────────────────────────────────────────


async def dataset_rolling(
    registry: DatasetRegistry,
    handle: str,
    column: str,
    window: int,
    op: str = "mean",
) -> dict:
    """Rolling statistic over a numeric column → stored as a NEW dataset.

    ``op``: ``mean | std | min | max | sum``. The result frame is the source
    plus a ``<column>_rolling_<op>_<window>`` column; returns its handle.
    """
    try:
        df = registry.get(handle)
    except DatasetError as exc:
        return {"error": str(exc)}
    if column not in df.columns:
        return {"error": f"column {column!r} not in dataset; columns: {list(df.columns)}"}
    if not pd.api.types.is_numeric_dtype(df[column]):
        return {
            "error": f"column {column!r} is {df[column].dtype}, not numeric — "
            "rolling stats need a numeric column"
        }
    if op not in ("mean", "std", "min", "max", "sum"):
        return {"error": f"invalid op {op!r}; use mean|std|min|max|sum"}
    window = max(2, int(window))
    out = df.copy()
    out_col = f"{column}_rolling_{op}_{window}"
    out[out_col] = getattr(df[column].rolling(window), op)()
    new_handle = registry.put(out)
    meta = registry.describe(new_handle)
    meta["derived"] = f"rolling {op} (window={window}) of {column!r} → column {out_col!r}"
    return meta


# ── Analysis: dataset_correlation ──────────────────────────────────────


async def dataset_correlation(
    registry: DatasetRegistry, handle: str, col_a: str, col_b: str
) -> dict:
    """Pearson correlation between two numeric columns of one dataset.

    Operates within a single loaded frame — e.g. volume vs daily_return
    on a price dataset.
    """
    try:
        df = registry.get(handle)
    except DatasetError as exc:
        return {"error": str(exc)}
    for c in (col_a, col_b):
        if c not in df.columns:
            return {"error": f"column {c!r} not in dataset; columns: {list(df.columns)}"}
        if not pd.api.types.is_numeric_dtype(df[c]):
            return {
                "error": f"column {c!r} is {df[c].dtype}, not numeric — "
                "correlation needs numeric columns"
            }
    pair = df[[col_a, col_b]].dropna()
    if len(pair) < 3:
        return {"error": f"only {len(pair)} rows with both columns present — too few"}
    return {
        "handle": handle,
        "col_a": col_a,
        "col_b": col_b,
        "pearson_r": scalar(pair[col_a].corr(pair[col_b])),
        "n": int(len(pair)),
    }


# ── Analysis: dataset_arima ────────────────────────────────────────────


async def dataset_arima(
    registry: DatasetRegistry, handle: str, column: str, horizon: int = 10
) -> dict:
    """ARIMA(1,0,1) forecast of the next ``horizon`` values of a numeric column.

    Naive baseline — not a research-grade forecast.
    """
    try:
        df = registry.get(handle)
    except DatasetError as exc:
        return {"error": str(exc)}
    if column not in df.columns:
        return {"error": f"column {column!r} not in dataset; columns: {list(df.columns)}"}
    if not pd.api.types.is_numeric_dtype(df[column]):
        return {
            "error": f"column {column!r} is {df[column].dtype}, not numeric — "
            "ARIMA needs a numeric series"
        }
    series = [float(x) for x in df[column].dropna().tolist()]
    if len(series) < 30:
        return {"error": f"only {len(series)} non-null values — need >=30 to fit ARIMA"}
    horizon = max(1, min(int(horizon), 30))

    def _fit() -> dict:
        # Lazy import — statsmodels is heavy.
        from statsmodels.tsa.arima.model import ARIMA

        fitted = ARIMA(series, order=(1, 0, 1)).fit()
        fc = fitted.get_forecast(steps=horizon)
        ci = fc.conf_int(alpha=0.05)
        return {
            "mean": [float(x) for x in fc.predicted_mean],
            "lower_95": [float(row[0]) for row in ci],
            "upper_95": [float(row[1]) for row in ci],
            "aic": float(fitted.aic),
        }

    try:
        fit = await asyncio.to_thread(_fit)
    except Exception as exc:
        return {"error": f"ARIMA fit failed: {exc}"}
    return {
        "handle": handle,
        "column": column,
        "horizon": horizon,
        "history_points": len(series),
        "forecast": [
            {
                "step": i + 1,
                "predicted": fit["mean"][i],
                "lower_95": fit["lower_95"][i],
                "upper_95": fit["upper_95"][i],
            }
            for i in range(horizon)
        ],
        "model": "ARIMA(1,0,1)",
        "aic": fit["aic"],
        "caveat": "naive baseline; not a research-grade forecast",
    }


# ── Registry ───────────────────────────────────────────────────────────


TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "headline_search",
            "description": (
                "Full-text search over the headline corpora; returns the matching "
                "headline text directly (at most 50 rows). Filter by source "
                "(`abc`, `analyst`, `partner`, `all`), ticker (US sources only), and date range."
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
                        "description": "Filter to a ticker (US sources only).",
                    },
                    "start_date": {"type": "string", "description": "ISO YYYY-MM-DD."},
                    "end_date": {"type": "string", "description": "ISO YYYY-MM-DD."},
                    "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 50},
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
            "description": "Find S&P 500 securities by ticker symbol (exact) or company name (fuzzy).",
            "parameters": {
                "type": "object",
                "properties": {"name_or_ticker": {"type": "string"}},
                "required": ["name_or_ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_prices",
            "description": (
                "Load a ticker's daily OHLCV + derived `daily_return` into a dataset. "
                "Returns a handle + shape/schema/head sample, NOT the rows. Then use the "
                "dataset_* analysis tools on the handle. Price data covers 2010-01-04..2016-12-30."
            ),
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
            "name": "dataset_describe",
            "description": "Per-column summary statistics (count, mean, std, min/max, quartiles) for a loaded dataset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "A dataset handle, e.g. ds_1."}
                },
                "required": ["handle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dataset_sample",
            "description": (
                "Return a small bounded slice of actual rows from a dataset (max 50). "
                "Optionally sort by a column first — e.g. sort by daily_return ascending "
                "for the worst days. The only way raw rows reach you; always bounded."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "n": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    "sort_by": {
                        "type": "string",
                        "description": "Column to sort by before sampling.",
                    },
                    "descending": {"type": "boolean", "default": False},
                },
                "required": ["handle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dataset_rolling",
            "description": (
                "Compute a rolling statistic over a numeric column. Produces a NEW dataset "
                "(source + the rolling column) and returns its handle."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "column": {"type": "string"},
                    "window": {"type": "integer", "description": "Rolling window size (>=2)."},
                    "op": {
                        "type": "string",
                        "enum": ["mean", "std", "min", "max", "sum"],
                        "default": "mean",
                    },
                },
                "required": ["handle", "column", "window"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dataset_correlation",
            "description": "Pearson correlation between two numeric columns of one dataset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "col_a": {"type": "string"},
                    "col_b": {"type": "string"},
                },
                "required": ["handle", "col_a", "col_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dataset_arima",
            "description": (
                "ARIMA(1,0,1) forecast of the next N values of a numeric column in a dataset. "
                "Naive baseline — not a research-grade forecast. There is no support for other "
                "models (LSTM, XGBoost, etc.); decline such requests rather than improvising."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "column": {"type": "string", "description": "Numeric column to forecast."},
                    "horizon": {"type": "integer", "default": 10, "minimum": 1, "maximum": 30},
                },
                "required": ["handle", "column"],
            },
        },
    },
]


_ToolFn = Callable[..., Awaitable[dict]]

_DISPATCH: dict[str, _ToolFn] = {
    "headline_search": headline_search,
    "headline_topic_frequency": headline_topic_frequency,
    "lookup_security": lookup_security,
    "load_prices": load_prices,
    "dataset_describe": dataset_describe,
    "dataset_sample": dataset_sample,
    "dataset_rolling": dataset_rolling,
    "dataset_correlation": dataset_correlation,
    "dataset_arima": dataset_arima,
}

# Tools that operate on the per-run dataset registry — dispatch injects it.
_REGISTRY_TOOLS = {
    "load_prices",
    "dataset_describe",
    "dataset_sample",
    "dataset_rolling",
    "dataset_correlation",
    "dataset_arima",
}


def _validate_registry_tools() -> None:
    """Fail fast at import if _REGISTRY_TOOLS disagrees with the tool signatures.

    A registry-aware tool whose name is missing from _REGISTRY_TOOLS would
    otherwise only fail when the agent first calls it — ``dispatch`` would call
    it without the injected ``registry``, raising a missing-argument
    ``TypeError`` mid-run. Validating here means a misconfigured registry stops
    the service from starting, before any agent interaction.
    """
    needs_registry = {
        name
        for name, fn in _DISPATCH.items()
        if next(iter(inspect.signature(fn).parameters), None) == "registry"
    }
    if needs_registry != _REGISTRY_TOOLS:
        missing = sorted(needs_registry - _REGISTRY_TOOLS)
        extra = sorted(_REGISTRY_TOOLS - needs_registry)
        problems = []
        if missing:
            problems.append(f"missing from _REGISTRY_TOOLS (they take `registry`): {missing}")
        if extra:
            problems.append(f"in _REGISTRY_TOOLS but take no `registry` arg: {extra}")
        raise RuntimeError("tool registry misconfigured — " + "; ".join(problems))


# Runs at import — i.e. at service startup, before any request is served.
_validate_registry_tools()


async def dispatch(name: str, args: dict, registry: DatasetRegistry) -> dict:
    """Run the named tool. Returns a JSON-serialisable dict (or an error dict).

    Registry-aware tools (loaders + analysis) receive ``registry`` as their
    first argument; inline tools do not.
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        if name in _REGISTRY_TOOLS:
            return await fn(registry, **(args or {}))
        return await fn(**(args or {}))
    except TypeError as exc:
        # Bad argument shape — surface it so the model can correct.
        return {"error": f"argument error: {exc}"}
    except Exception as exc:
        logger.exception("tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


def tool_result_text(result: Any) -> str:
    """Render a tool result for the tool message body (LiteLLM expects text)."""
    return json.dumps(_json_safe(result), default=str)
