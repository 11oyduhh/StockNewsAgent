# StockNewsAgent

A small but production-shaped **agent platform** over news headlines and S&P 500
financial data. Give it a natural-language task; it plans with an LLM, queries
Postgres through a fixed set of tools, and returns an answer — with every step
traced. The whole stack comes up with one `docker compose` command.

## Prerequisites

- **Docker** — Docker Desktop on macOS/Windows, or Docker Engine + Compose v2.
- An **Anthropic API key**.
- **Python 3** on the host, to run the `agent.py` client — standard library
  only, nothing to install.

## Quick start

```bash
cp .env.example .env          # then set ANTHROPIC_API_KEY
docker compose up --build     # postgres + pgbouncer + ingest + agent
```

The first run ingests ~680 MB of CSV data into Postgres (a few minutes); later
runs detect the data is already loaded and skip, so startup is fast. Once the
`agent` service is healthy:

```bash
python agent.py "How volatile was AAPL in 2013, and what were its 3 worst days?"
python agent.py "How many headlines mention 'bushfire'?"
python agent.py "Did news about Apple line up with its 2014 price moves?" --trace
```

`--trace` streams the run live — each LLM call and tool call (with the model's
reasoning) prints the moment it completes, so you can watch the agent work and
`Ctrl-C` to abort it. `python agent.py --trace-only <task_id>` inspects a past
run without re-running it.

## What it can do

The agent works through a fixed, deliberate set of tools:

- **Headlines** — full-text search and time-bucketed frequency counts across
  ~1.24M Australian ABC News headlines and ~3.2M US ticker-tagged headlines.
- **Prices** — load a ticker's daily OHLCV + returns, then run summary
  statistics, sampling, rolling statistics, Pearson correlation, or an ARIMA
  baseline forecast.
- **Securities** — look up S&P 500 companies by name or symbol.

Heavy data is loaded server-side and the agent operates on a *handle*, so large
results never overflow the model's context. Requests outside the tool set
(train an LSTM, run arbitrary SQL) are declined rather than faked — see
[`DECISIONS.md`](DECISIONS.md).

## How it works

`docker compose up` starts four services:

| Service | Role |
|---|---|
| `postgres` | Postgres 16 — data tables, full-text indexes, and the `traces` table |
| `pgbouncer` | connection pooler in front of Postgres |
| `ingest` | one-shot job — streams the CSVs into Postgres, then exits (idempotent) |
| `agent` | FastAPI service — the agent loop, tools, compaction, telemetry |

`agent.py` is a thin host-side client that POSTs tasks to the `agent` service
over HTTP. Full detail in the docs below.

## Data setup

The six source CSVs (~680 MB) are **not** committed (they're `.gitignore`d).
Place them in the `datasets/` folder before `docker compose up` — the ingest
container mounts that folder and loads everything in it:

- `datasets/abcnews-date-text.csv`
- `datasets/analyst_ratings_processed.csv`
- `datasets/raw_partner_headlines.csv`
- `datasets/prices-split-adjusted.csv`
- `datasets/securities.csv`
- `datasets/fundamentals.csv`

## Development

Host-side tooling is managed with [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync                              # create .venv with runtime + dev deps
uv run black .                       # format
uv run flake8                        # lint
uv run mypy service ingest agent.py  # type-check
```

## Docs

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system diagrams
- [`WALKTHROUGH.md`](WALKTHROUGH.md) — how it works, in prose
- [`DECISIONS.md`](DECISIONS.md) — what was built, tradeoffs, scaling toward production
