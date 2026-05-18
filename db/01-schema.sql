-- Schema for the Anthelion take-home agent platform.
--
-- Loaded once by the postgres image on first boot via
-- /docker-entrypoint-initdb.d/. Idempotent DDL only — no secrets.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()


-- ── Headlines ──────────────────────────────────────────────────────────
-- Australian general news. Kept separate from us_headlines because the
-- domain and schema differ (no ticker, just a date).
CREATE TABLE IF NOT EXISTS abc_headlines (
    id              BIGSERIAL PRIMARY KEY,
    publish_date    DATE NOT NULL,
    headline        TEXT NOT NULL,
    headline_tsv    tsvector GENERATED ALWAYS AS (to_tsvector('english', headline)) STORED,
    UNIQUE (publish_date, headline)
);
CREATE INDEX IF NOT EXISTS abc_headlines_fts_idx ON abc_headlines USING GIN (headline_tsv);
CREATE INDEX IF NOT EXISTS abc_headlines_date_idx ON abc_headlines (publish_date);


-- US ticker-tagged headlines. Two sources unioned with a discriminator:
--   'analyst' — analyst_ratings_processed.csv
--   'partner' — raw_partner_headlines.csv (also carries publisher attribution)
CREATE TABLE IF NOT EXISTS us_headlines (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('analyst', 'partner')),
    published_at    TIMESTAMPTZ NOT NULL,
    headline        TEXT NOT NULL,
    ticker          TEXT,
    publisher       TEXT,
    headline_tsv    tsvector GENERATED ALWAYS AS (to_tsvector('english', headline)) STORED,
    -- NULLS NOT DISTINCT (PG 15+) so two otherwise-identical rows with NULL
    -- ticker dedupe on re-ingest. Default UNIQUE semantics treat each NULL
    -- as distinct, which would break idempotency for our purposes.
    UNIQUE NULLS NOT DISTINCT (source, published_at, headline, ticker)
);
CREATE INDEX IF NOT EXISTS us_headlines_fts_idx       ON us_headlines USING GIN (headline_tsv);
CREATE INDEX IF NOT EXISTS us_headlines_published_idx ON us_headlines (published_at);
CREATE INDEX IF NOT EXISTS us_headlines_ticker_idx    ON us_headlines (ticker) WHERE ticker IS NOT NULL;


-- ── Securities reference data ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS securities (
    ticker              TEXT PRIMARY KEY,
    security_name       TEXT NOT NULL,
    sec_filings         TEXT,
    sector              TEXT,
    sub_industry        TEXT,
    address             TEXT,
    date_first_added    DATE,
    cik                 TEXT
);
CREATE INDEX IF NOT EXISTS securities_sector_idx ON securities (sector);


-- ── Prices (split-adjusted daily) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS prices (
    ticker      TEXT NOT NULL,
    date        DATE NOT NULL,
    open        NUMERIC NOT NULL,
    close       NUMERIC NOT NULL,
    low         NUMERIC NOT NULL,
    high        NUMERIC NOT NULL,
    volume      BIGINT NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS prices_date_idx ON prices (date);


-- ── Fundamentals ───────────────────────────────────────────────────────
-- 79-column wide source with awkward header names. Stored as JSONB so
-- the agent can query specific metrics via data->>'Total Revenue' without
-- a brittle column-name mapping at ingest. for_year is hoisted out
-- because it's the most natural filter.
CREATE TABLE IF NOT EXISTS fundamentals (
    ticker      TEXT NOT NULL,
    period_end  DATE NOT NULL,
    for_year    INT,
    data        JSONB NOT NULL,
    PRIMARY KEY (ticker, period_end)
);
CREATE INDEX IF NOT EXISTS fundamentals_for_year_idx ON fundamentals (for_year);


-- ── Observability: traces ──────────────────────────────────────────────
-- One row per LLM call OR tool call. Lets us replay any task end-to-end
-- and slice cost / latency / errors by task class, model, tool, etc.
CREATE TABLE IF NOT EXISTS traces (
    id              BIGSERIAL PRIMARY KEY,
    task_id         UUID NOT NULL,
    turn_index      INT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind            TEXT NOT NULL CHECK (kind IN ('llm_call', 'tool_call')),
    model           TEXT,
    -- llm_call rows: prompt (messages array) + response (assistant message)
    prompt          JSONB,
    response        JSONB,
    -- tool_call rows: tool name + input args + output payload
    tool_name       TEXT,
    tool_input      JSONB,
    tool_output     JSONB,
    -- metrics
    input_tokens    INT,
    output_tokens   INT,
    cost_usd        NUMERIC(12, 6),
    latency_ms      INT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS traces_task_idx ON traces (task_id, turn_index);
CREATE INDEX IF NOT EXISTS traces_ts_idx   ON traces (ts);
