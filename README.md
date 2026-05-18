# StockNewsAgent

An agent platform over news headlines and S&P 500 financial data: give it a
natural-language task, it makes LLM calls, queries Postgres, and returns an
answer — all running via one `docker compose` command.

> Brief placeholder README — fuller content to come.

## Quick start

```bash
cp .env.example .env          # then set ANTHROPIC_API_KEY
docker compose up --build     # postgres + pgbouncer + ingest + agent
python agent.py "headlines mentioning Apple in March 2014"
python agent.py "how volatile was AAPL in 2013?" --trace   # + telemetry timeline
```

`--trace` prints the run's step-by-step telemetry (every LLM call and tool call,
with the model's reasoning); `--trace-only <task_id>` inspects a past run.

## Data setup

The six source CSVs (~2 GB) are **not** committed (they're `.gitignore`d).
Place them in the repo root before `docker compose up`:

- `abcnews-date-text.csv`
- `analyst_ratings_processed.csv`
- `raw_partner_headlines.csv`
- `prices-split-adjusted.csv`
- `securities.csv`
- `fundamentals.csv`

## Docs

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system diagrams
- [`WALKTHROUGH.md`](WALKTHROUGH.md) — how it works, in prose
- [`DECISIONS.md`](DECISIONS.md) — what was built, tradeoffs, scaling toward production
