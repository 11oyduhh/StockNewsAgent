# Walkthrough (how it all works)

A plain-English tour of the system. Diagrams are in
[ARCHITECTURE.md](ARCHITECTURE.md); the *why* behind each choice is in
[DECISIONS.md](DECISIONS.md).

## What this is

A small but production-shaped agent platform. You give it a
natural-language task; it makes one or more LLM calls, queries a
database of news headlines and S&P 500 financial data, and returns an
answer. The whole thing comes up with one command and is exercised end
to end by a single CLI script.

## One-command bring-up

```bash
docker compose up --build      # starts the stack
python agent.py "your task"    # asks the agent a question
```

`docker compose up` builds and starts four services. On first run it
also ingests ~650 MB of CSV data into Postgres (a few minutes); on
later runs ingestion detects the data is already there and skips, so
startup is fast.

## The four services

Everything runs in containers defined by `docker-compose.yml`:

1. **postgres** — Postgres 16. Holds the data tables, the full-text
   search indexes, and the `traces` observability table. Its schema is
   created on first boot from `db/01-schema.sql`; a read-only role for
   the agent's `sql()` tool is created by `db/02-setup-roles.sh`.

2. **pgbouncer** — a connection pooler in front of Postgres. Each real
   Postgres connection is relatively expensive; pgbouncer lets the
   agent open cheap connections and multiplexes them onto a small pool.
   It fronts the agent's *writer* traffic — the hot path, and the place
   production concerns (scaling, connection limits) attach.

3. **ingest** — a one-shot job. It streams the six CSV files into
   Postgres, then exits. It is idempotent: re-running it skips tables
   that already hold data. The agent service waits for ingest to finish
   successfully before it starts.

4. **agent** — the FastAPI service that *is* the agent. It exposes
   `POST /run` (execute a task) and `GET /healthz` (liveness). This is
   where the LLM loop, the tools, compaction, and telemetry live.

A host-side script, `agent.py`, is the client. It has no third-party
dependencies — it just POSTs your task to the service and prints the
answer plus a short run summary (turns, tokens, cost).

## Ingestion

The data is six CSVs: ~1.24M Australian ABC News headlines, ~3.2M US
ticker-tagged headlines (two sources), daily S&P 500 prices
(2010–2016), S&P 500 security metadata, and per-ticker fundamentals.

`ingest/ingest.py` streams each file into Postgres using `COPY` (far
faster than row-by-row inserts) via a temporary staging table, then
folds the staged rows into the real table with
`INSERT ... ON CONFLICT DO NOTHING`. That `ON CONFLICT` clause is what
makes re-runs safe — duplicate rows are silently dropped.

Two notable shape decisions: the 79-column `fundamentals` data is
stored as a single `JSONB` column rather than 79 typed columns (the
upstream headers are too irregular to be worth a brittle mapping);
and the two US headline sources are unioned into one `us_headlines`
table with a `source` discriminator column.

## The agent request lifecycle

When you run `python agent.py "task"`:

1. The CLI POSTs `{task}` to the FastAPI service.
2. The service hands the task to `run_agent()` in `service/loop.py`.
3. The loop seeds a conversation: a system prompt (which describes the
   data, the tools, and known limitations) plus your task.
4. Then it loops:
   - **Maybe compact.** If the conversation has grown past a token
     threshold, older turns are summarized down (see below).
   - **Call the model.** LiteLLM sends the conversation plus the list
     of 8 tool definitions to Claude.
   - **Branch.** If the model replied with no tool calls, it has an
     answer — the loop returns it. If it asked to call tools, the loop
     dispatches each one, appends the results to the conversation, and
     goes around again.
   - A round budget (`max_rounds_with_tool_calls`) caps the tool-use
     loop. If it runs out with tool calls still pending, one final
     tool-free call lets the model synthesize an answer from what it
     has — so a capped run still returns something coherent rather
     than a stub.
5. The service returns the answer plus a run summary (turn count,
   tokens, cost) to the CLI, which prints it.

## The 8 tools

Tools are how the agent actually *uses the data*. Each is an async
Python function in `service/tools.py` with a JSON schema the model
sees. Seven are structured and scoped; the eighth is an escape hatch.

| Tool | What it does |
|---|---|
| `headline_search` | Full-text search across the headline corpora, with date/ticker/source filters |
| `headline_topic_frequency` | Counts of matching headlines bucketed by day/week/month/year |
| `lookup_security` | Find a ticker by company name or symbol |
| `price_history` | Daily OHLCV for a ticker over a date range |
| `returns` | Daily close-to-close percentage returns |
| `forecast_returns` | A naive ARIMA forecast of next-N daily returns |
| `fundamentals_lookup` | Pull a named metric (e.g. "Total Revenue") from the JSONB fundamentals |
| `sql` | Read-only SELECT/WITH escape hatch for anything the above don't cover |

Full-text search uses Postgres `tsvector` with GIN indexes — real
keyword search with stemming and ranking, not `LIKE` scans.

The `sql()` tool is deliberately constrained: it runs through a
separate **read-only database role**, with a per-connection statement
timeout and a row cap on results. Even if the model writes a wild
query, it cannot write data, cannot run forever, and cannot return an
unbounded result.

## Context compaction

Long tool-using conversations can blow past the model's context
window. `service/compaction.py` handles this. When cumulative input
tokens cross a configurable threshold, it summarizes the older turns
into a compact `<summary>` block and keeps the most recent turns
verbatim.

The subtle part is **tool-pair safety**: a tool result must always
follow the assistant message that requested it, or the API rejects the
conversation. So before cutting, the compactor "walks back" the cut
point until no tool result is left orphaned. A final integrity check
backs this up — if a cut would still produce an orphan, it abandons
the compaction rather than send a broken conversation.

This is a simplified adaptation of a pattern from a larger production
agent codebase.

## Telemetry and traces

Every LLM call and every tool call is recorded in the Postgres
`traces` table — one row each, carrying the task id, turn index,
model, prompt/response (or tool input/output), token counts, cost,
latency, and any error.

LLM calls are captured by a LiteLLM callback (`service/telemetry.py`):
a `CustomLogger` subclass that fires on every completion, success or
failure, and writes a row. Tool calls are recorded by the agent loop
directly. The result: any task can be replayed and audited after the
fact, and cost/latency/error rates can be sliced by model or tool.
The telemetry write is wrapped defensively — a logging failure can
never break the actual agent run.

## Where to look in the code

```
docker-compose.yml      the four services, wired together
db/01-schema.sql        tables, GIN full-text indexes, traces table
db/02-setup-roles.sh    the read-only role for the sql() tool
ingest/ingest.py        streamed CSV → Postgres
service/main.py         FastAPI app — POST /run, GET /healthz
service/loop.py         the agent loop (the heart of it)
service/tools.py        the 8 tools + dispatcher
service/compaction.py   context compaction
service/telemetry.py    LiteLLM → traces callback
service/db.py           the writer + reader connection pools
service/prompts.py      the system prompt
agent.py                the host-side CLI client
```

## What was left out, on purpose

Semantic search / embeddings, MCP servers, web fetch, and
authentication are all deliberately out of scope — see DECISIONS.md
for the reasoning and how each would be added when scaling toward
production.
