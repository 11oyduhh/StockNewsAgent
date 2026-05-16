"""One-shot ingestion: CSVs at /data → Postgres.

Streams big CSVs via psycopg.copy() into TEMP staging tables, then folds
into the real tables with INSERT … ON CONFLICT DO NOTHING for idempotent
re-runs. Small CSVs (securities, fundamentals) use executemany — same
ON CONFLICT semantics, simpler code.

A row-count guard short-circuits each stage when the target table already
holds data, so `docker compose up` after the first run completes in
seconds. Set FORCE_REINGEST=1 to override.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import psycopg

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
FORCE_REINGEST = os.environ.get("FORCE_REINGEST") == "1"


def connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ["POSTGRES_PORT"]),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
    )


def _populated(cur: psycopg.Cursor, table: str) -> bool:
    cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
    return cur.fetchone() is not None


def _to_native(v: Any) -> Any:
    """Coerce numpy / pandas scalars to JSON-friendly Python types."""
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


# ────────────────────────────────────────────────────────────────────────
# abc_headlines (Australian, ~1.24M rows)
#   CSV: publish_date (YYYYMMDD int), headline_text
# ────────────────────────────────────────────────────────────────────────
def ingest_abc_headlines(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        if _populated(cur, "abc_headlines") and not FORCE_REINGEST:
            return 0
        cur.execute(
            "CREATE TEMP TABLE _stage_abc (publish_date DATE, headline TEXT) " "ON COMMIT DROP"
        )
        path = DATA_DIR / "abcnews-date-text.csv"
        with open(path, "r", encoding="utf-8") as fh:
            next(fh)  # header
            with cur.copy("COPY _stage_abc (publish_date, headline) FROM STDIN") as cp:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    date_int, _, headline = line.partition(",")
                    if len(date_int) != 8 or not date_int.isdigit():
                        continue
                    iso = f"{date_int[0:4]}-{date_int[4:6]}-{date_int[6:8]}"
                    cp.write_row((iso, headline))
        cur.execute(
            """
            INSERT INTO abc_headlines (publish_date, headline)
            SELECT publish_date, headline FROM _stage_abc
            ON CONFLICT (publish_date, headline) DO NOTHING
            """
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


# ────────────────────────────────────────────────────────────────────────
# us_headlines from analyst_ratings_processed.csv (~1.4M rows)
#   CSV: ,title,date,stock     date is ISO with tz
# ────────────────────────────────────────────────────────────────────────
def ingest_us_headlines_analyst(conn: psycopg.Connection) -> int:
    return _ingest_us_headlines(
        conn,
        path=DATA_DIR / "analyst_ratings_processed.csv",
        source="analyst",
        row_to_tuple=lambda r: _analyst_row(r),
        stage_name="_stage_us_analyst",
    )


def _analyst_row(row: list[str]) -> tuple | None:
    # row: [idx, title, date, stock]
    if len(row) < 4:
        return None
    _idx, title, date, stock = row[:4]
    if not title or not date:
        return None
    return ("analyst", date, title, stock or None, None)


# ────────────────────────────────────────────────────────────────────────
# us_headlines from raw_partner_headlines.csv (~1.8M rows)
#   CSV: ,headline,url,publisher,date,stock     date is naive (UTC assumed)
# ────────────────────────────────────────────────────────────────────────
def ingest_us_headlines_partner(conn: psycopg.Connection) -> int:
    return _ingest_us_headlines(
        conn,
        path=DATA_DIR / "raw_partner_headlines.csv",
        source="partner",
        row_to_tuple=lambda r: _partner_row(r),
        stage_name="_stage_us_partner",
    )


def _partner_row(row: list[str]) -> tuple | None:
    # row: [idx, headline, url, publisher, date, stock]
    if len(row) < 6:
        return None
    _idx, headline, _url, publisher, date, stock = row[:6]
    if not headline or not date:
        return None
    return ("partner", date, headline, stock or None, publisher or None)


def _ingest_us_headlines(
    conn: psycopg.Connection,
    *,
    path: Path,
    source: str,
    row_to_tuple: Callable[[list[str]], tuple | None],
    stage_name: str,
) -> int:
    with conn.cursor() as cur:
        # We can't cheap-skip by querying us_headlines (other source may exist).
        # Probe by (source) to decide whether THIS half has already been done.
        cur.execute("SELECT 1 FROM us_headlines WHERE source = %s LIMIT 1", (source,))
        already = cur.fetchone() is not None
        if already and not FORCE_REINGEST:
            return 0

        cur.execute(
            f"""
            CREATE TEMP TABLE {stage_name} (
                source TEXT, published_at TIMESTAMPTZ, headline TEXT,
                ticker TEXT, publisher TEXT
            ) ON COMMIT DROP
            """
        )
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            with cur.copy(
                f"COPY {stage_name} "
                "(source, published_at, headline, ticker, publisher) "
                "FROM STDIN"
            ) as cp:
                for row in reader:
                    tup = row_to_tuple(row)
                    if tup is not None:
                        cp.write_row(tup)
        cur.execute(
            """
            INSERT INTO us_headlines
                (source, published_at, headline, ticker, publisher)
            SELECT source, published_at, headline, ticker, publisher
            FROM """
            + stage_name
            + """
            ON CONFLICT (source, published_at, headline, ticker) DO NOTHING
            """
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


# ────────────────────────────────────────────────────────────────────────
# securities (~500 rows) — small, pandas + executemany.
# ────────────────────────────────────────────────────────────────────────
def ingest_securities(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        if _populated(cur, "securities") and not FORCE_REINGEST:
            return 0
    df = pd.read_csv(DATA_DIR / "securities.csv")
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(
        columns={
            "Ticker symbol": "ticker",
            "Security": "security_name",
            "SEC filings": "sec_filings",
            "GICS Sector": "sector",
            "GICS Sub Industry": "sub_industry",
            "Address of Headquarters": "address",
            "Date first added": "date_first_added",
            "CIK": "cik",
        }
    )
    df = df[
        [
            "ticker",
            "security_name",
            "sec_filings",
            "sector",
            "sub_industry",
            "address",
            "date_first_added",
            "cik",
        ]
    ]
    df["date_first_added"] = pd.to_datetime(df["date_first_added"], errors="coerce").dt.date
    df["cik"] = df["cik"].astype("object").where(df["cik"].notna(), None)
    rows = [tuple(_to_native(v) for v in r) for r in df.itertuples(index=False, name=None)]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO securities (
                ticker, security_name, sec_filings, sector,
                sub_industry, address, date_first_added, cik
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker) DO NOTHING
            """,
            rows,
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


# ────────────────────────────────────────────────────────────────────────
# prices-split-adjusted (~850K rows)
#   CSV: date,symbol,open,close,low,high,volume → table cols match after
#   the symbol→ticker rename. Stream the file directly through COPY CSV.
# ────────────────────────────────────────────────────────────────────────
def ingest_prices(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        if _populated(cur, "prices") and not FORCE_REINGEST:
            return 0
        # volume is staged as NUMERIC because the source CSV writes it as
        # float-formatted ints ("2163600.0", a pandas roundtrip artifact in
        # the upstream dataset). NUMERIC accepts that on COPY; we cast to
        # BIGINT in the INSERT below.
        cur.execute(
            """
            CREATE TEMP TABLE _stage_prices (
                date DATE, ticker TEXT, open NUMERIC, close NUMERIC,
                low NUMERIC, high NUMERIC, volume NUMERIC
            ) ON COMMIT DROP
            """
        )
        with open(DATA_DIR / "prices-split-adjusted.csv", "rb") as fh:
            with cur.copy("COPY _stage_prices FROM STDIN WITH (FORMAT CSV, HEADER true)") as cp:
                while chunk := fh.read(1 << 16):
                    cp.write(chunk)
        cur.execute(
            """
            INSERT INTO prices (ticker, date, open, close, low, high, volume)
            SELECT ticker, date, open, close, low, high, volume::bigint
            FROM _stage_prices
            WHERE volume IS NOT NULL
            ON CONFLICT (ticker, date) DO NOTHING
            """
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


# ────────────────────────────────────────────────────────────────────────
# fundamentals (~1782 rows, 79 cols) — JSONB.
# ────────────────────────────────────────────────────────────────────────
def ingest_fundamentals(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        if _populated(cur, "fundamentals") and not FORCE_REINGEST:
            return 0
    df = pd.read_csv(DATA_DIR / "fundamentals.csv")
    # Normalize headers — some contain embedded whitespace/newlines.
    df.columns = [c.strip().replace("\n", " ") for c in df.columns]
    df = df.rename(columns={"Ticker Symbol": "ticker", "Period Ending": "period_end"})
    df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce").dt.date
    for_year_col = "For Year" if "For Year" in df.columns else None

    rows: list[tuple] = []
    for _, row in df.iterrows():
        ticker = row.get("ticker")
        period_end = row.get("period_end")
        if not isinstance(ticker, str) or period_end is None:
            continue
        for_year = None
        if for_year_col is not None and pd.notna(row.get(for_year_col)):
            for_year = int(row[for_year_col])
        data = {k: _to_native(v) for k, v in row.items() if k not in ("ticker", "period_end")}
        rows.append((ticker, period_end, for_year, json.dumps(data)))

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fundamentals (ticker, period_end, for_year, data)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (ticker, period_end) DO NOTHING
            """,
            rows,
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


def main() -> int:
    host = os.environ["POSTGRES_HOST"]
    port = os.environ["POSTGRES_PORT"]
    print(f"[ingest] connecting to {host}:{port}", flush=True)
    print(f"[ingest] data dir: {DATA_DIR}  (FORCE_REINGEST={FORCE_REINGEST})", flush=True)

    stages: list[tuple[str, Callable[[psycopg.Connection], int]]] = [
        ("securities", ingest_securities),
        ("prices", ingest_prices),
        ("fundamentals", ingest_fundamentals),
        ("abc_headlines", ingest_abc_headlines),
        ("us_headlines:analyst", ingest_us_headlines_analyst),
        ("us_headlines:partner", ingest_us_headlines_partner),
    ]
    with connect() as conn:
        for name, fn in stages:
            t0 = time.time()
            try:
                inserted = fn(conn)
            except Exception as exc:
                print(f"[ingest] {name}: FAILED — {exc}", flush=True)
                return 1
            dt = time.time() - t0
            verb = "inserted" if inserted else "skipped (already populated)"
            print(f"[ingest] {name}: {verb} {inserted or ''} rows in {dt:.1f}s", flush=True)

    print("[ingest] done.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
