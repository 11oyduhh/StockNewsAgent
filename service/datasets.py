"""Per-run dataset registry.

The loader (``load_prices``) pulls a query result
into a pandas DataFrame held here and returns only a *handle* plus the frame's
shape / schema / a tiny head sample. Analysis tools (``dataset_*``) then
operate on the handle. The agent never receives bulk rows — large tool
results are eliminated by construction rather than capped after the fact.

Lifecycle: one :class:`DatasetRegistry` per ``run_agent`` call, cleared when
the run ends. In-process memory only — see DECISIONS.md for why per-run
scoping is sufficient here and what the production path (a shared store)
looks like.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd


class DatasetError(Exception):
    """Raised for an unknown/missing handle — surfaced to the agent as an error."""


class DatasetRegistry:
    """A per-run store of pandas DataFrames keyed by handle id."""

    def __init__(self) -> None:
        self._frames: dict[str, pd.DataFrame] = {}
        self._counter = 0

    def put(self, df: pd.DataFrame) -> str:
        """Store a frame, return a fresh handle id (``ds_1``, ``ds_2``, …)."""
        self._counter += 1
        handle = f"ds_{self._counter}"
        self._frames[handle] = df
        return handle

    def get(self, handle: str) -> pd.DataFrame:
        """Return the frame for ``handle`` or raise :class:`DatasetError`."""
        df = self._frames.get(handle)
        if df is None:
            avail = ", ".join(sorted(self._frames)) or "(none — load a dataset first)"
            raise DatasetError(f"unknown dataset handle {handle!r}; available: {avail}")
        return df

    def describe(
        self,
        handle: str,
        *,
        truncated: bool = False,
        total_rows: int | None = None,
        note: str | None = None,
    ) -> dict:
        """Metadata envelope the agent sees in place of bulk rows."""
        df = self.get(handle)
        head_n = int(os.environ.get("AGENT_DATASET_HEAD_ROWS", "5"))
        meta: dict[str, Any] = {
            "handle": handle,
            "n_rows": int(len(df)),
            "n_cols": int(df.shape[1]),
            "columns": [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns],
            "head": frame_records(df.head(head_n)),
        }
        if truncated:
            meta["truncated"] = True
            meta["total_rows"] = total_rows
            meta["note"] = note or "result was capped at AGENT_DATASET_MAX_ROWS rows"
        return meta

    def clear(self) -> None:
        """Drop all frames — called in a finally when the run ends."""
        self._frames.clear()

    @property
    def count(self) -> int:
        """How many datasets were created this run (monotonic)."""
        return self._counter

    def __len__(self) -> int:
        return len(self._frames)


def frame_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame → list-of-dicts with JSON-safe scalar values."""
    return [{str(k): scalar(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def scalar(v: Any) -> Any:
    """Coerce a pandas / numpy scalar to a JSON-friendly Python type."""
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass  # arrays / non-scalar — fall through
    if hasattr(v, "isoformat"):  # date / datetime / Timestamp
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalar
        return v.item()
    return v
