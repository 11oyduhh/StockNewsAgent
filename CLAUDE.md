# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## The take-home

Build a small but production-shaped agent platform around the `abcnews-date-text.csv` headlines dataset (~1.24M rows, `publish_date,headline_text`, dates as `YYYYMMDD` ints).

The agent must:
- Receive a task
- Make at least one LLM call
- Use the data to produce a response
- Run end-to-end via a single command

Deliverables expected at the repo root:
- `docker-compose.yml` — all services
- An agent script that exercises the full stack end-to-end
- `DECISIONS.md` — what was included/left out and why, tradeoffs, how it would evolve toward production, and how to scale for deployment

Evaluation criterion: identifying what a production-grade agent platform actually needs, wiring it together coherently, and articulating the reasoning. Lean toward decisions that demonstrate that judgment over feature completeness.

## Stack decisions (locked in by the user)

- **Language:** 100% Python where possible
- **LLM:** LiteLLM as the provider/framework abstraction
- **Storage:** Postgres 16 in-container, with `tsvector` FTS and a pgbouncer connection pooler in front. See DECISIONS.md for the SQLite→Postgres rationale (data volume + production-shape demonstration).
- **Context management / compaction:** deterministic compaction — summarize old turns, keep the recent tail verbatim, with tool_use↔tool_result pair-safety. Pure aggregation, no LLM-driven summarization. See `service/compaction.py`.
- **Telemetry:** a LiteLLM `CustomLogger` callback writing one row per LLM call to the Postgres `traces` table. See `service/telemetry.py`.
- **Secrets:** the user supplies a `.env` with API keys — read from env, do not hardcode, do not commit the file

## Repo layout

```
anthelion_take_home/
├── docker-compose.yml         # postgres + pgbouncer + ingest (one-shot) + agent (FastAPI)
├── .env.example               # template; copy to .env and fill ANTHROPIC_API_KEY
├── pyproject.toml + uv.lock    # single dependency source (host + both images)
├── datasets/                   # the 6 source CSVs (gitignored; ingest mounts this)
├── db/
│   └── 01-schema.sql          # tables, GIN tsvector indexes, traces table
├── ingest/                    # one-shot: streamed CSV → Postgres via psycopg.copy()
├── service/                   # FastAPI agent service
│   ├── main.py                # FastAPI lifespan + POST /run + GET /healthz + GET /traces/{id}
│   ├── loop.py                # agent loop (LiteLLM tool-use + compaction)
│   ├── tools.py               # 9-tool registry + dispatcher
│   ├── compaction.py          # deterministic conversation compaction
│   ├── telemetry.py           # LiteLLM CustomLogger → traces rows
│   ├── db.py                  # asyncpg connection pool (via pgbouncer)
│   └── prompts.py             # system prompt
└── agent.py                   # host-side CLI client (the deliverable)
```

Run with `docker compose up --build`; query with `python agent.py "..."`.

## Dev workflow (host-side)

`uv` manages every Python environment. `pyproject.toml` + `uv.lock` are the single dependency source of truth — host dev and both container builds resolve from the same lockfile, each syncing a subset:

- **host dev** — `uv sync` (project deps + the `dev` group)
- **service image** — `uv sync --frozen --no-dev` (project deps only)
- **ingest image** — `uv sync --frozen --only-group ingest` (just psycopg + pandas)

```bash
uv sync                          # create .venv with runtime + dev deps
uv run black .                   # auto-format
uv run flake8                    # lint
uv run mypy service ingest agent.py   # type-check
```

`flake8` reads its config from `.flake8` — flake8 has no native `pyproject.toml` support, and a dedicated file is portable without a plugin. Long-line rule is suppressed per-file for `service/prompts.py` and `service/tools.py` — both contain LLM-facing string content where line-length is a meaningless metric.

**In-scope datasets:**
- `abcnews-date-text.csv` — ~1.24M Australian ABC News headlines, 2003+
- `prices-split-adjusted.csv` — daily NYSE/S&P 500 prices, **2010-01-04 → 2016-12-30** (~500 tickers; we use split-adjusted, not the raw `prices.csv`)
- `securities.csv` — S&P 500 security metadata
- `fundamentals.csv` — periodic fundamentals per ticker
- **Pending: a US news dataset** with meaningful overlap of the 2010–2016 NYSE window, to make cause/effect demos work (ABC News is Australian and won't reliably mention US tickers)

Ingestion needs to be deliberate (streamed/batched), not a naive `pd.read_csv` into memory inside a container.

## When adding code

- Single-command bring-up is a hard requirement — `docker compose up` (or equivalent) must produce a working end-to-end demo, including ingestion if the DB isn't already populated.
- Favor a thin, legible vertical slice over breadth. The evaluator is looking for coherent platform thinking, not many features.
- `DECISIONS.md` is graded — keep notes on tradeoffs as you make them so writing it isn't archaeology at the end.
