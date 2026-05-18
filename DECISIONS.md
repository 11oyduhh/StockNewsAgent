# DECISIONS.md

A living record of architectural decisions for this take-home: what we chose, what we left out, the tradeoffs, and how the system would evolve toward production at scale. Updated as decisions are made.

## Scope

- **Datasets in scope:**
  - `abcnews-date-text.csv` — ~1.24M Australian ABC News headlines, 2003+
  - `analyst_ratings_processed.csv` — ~1.4M US ticker-tagged headlines, 2009-02 → 2020-06
  - `raw_partner_headlines.csv` — ~1.8M US ticker-tagged partner-publisher headlines, 2010-02 → 2020
  - `prices-split-adjusted.csv` — daily S&P 500 prices, 2010-01-04 → 2016-12-30, ~500 tickers
  - `securities.csv` — S&P 500 metadata
  - `fundamentals.csv` — per-ticker fundamentals
- **Excluded:** raw unadjusted `prices.csv` (split-adjusted is correct for returns); `raw_analyst_ratings.csv` (a wider duplicate of `analyst_ratings_processed.csv` we don't need).
- The two US headline corpora cover the full 2010–2016 NYSE window with years to spare, enabling real cause/effect analysis. ABC News stays as a second corpus to demonstrate multi-source tooling and honor the original brief.
- **Goal:** an end-to-end agent platform that takes a task, makes ≥1 LLM call, uses the data, and returns a response — runnable via `docker compose up` plus a thin host-side client script.

## Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Single-language stack; matches the LLM/data tooling. |
| LLM framework | LiteLLM, default `claude-sonnet-4-6` | Provider-portable, avoids vendor lock-in; Sonnet 4.6 is the cost/quality sweet spot for tool-using agents. |
| Agent surface | FastAPI (uvicorn) inside the agent container | An HTTP boundary is where production concerns (auth, rate limits, request tracing, multi-tenant routing) attach. CLI script (`agent.py`) on the host is a thin client over HTTP. |
| Storage | **Postgres 16** with `tsvector` FTS, fronted by **pgbouncer** | At ~650MB CSV / ~4.5M headlines, we're in real-data territory. Postgres in its own container with a connection pooler is what production looks like; SQLite would have worked but would force a doc-only scale story. `tsvector` is native, supports stemming + phrase ranking. |
| Forecasting | `statsmodels` ARIMA | Standard, lightweight; a defensible demo of a stats tool inside the agent loop. |
| Observability | Postgres `traces` table + structured stdout logs | Production-shaped at near-zero cost since Postgres is already in the stack. |
| Context management | Simplified port of the compaction pattern from `AgenticCRE` | Token-budget trigger, tail preservation, tool_use↔tool_result pair-safety walk-back, integrity backstop. CRE-specific signal extraction (URL/captcha/PIN/[KEY DECISION] regex) stripped out. |
| Telemetry | `litellm.callbacks` `CustomLogger` writing to the Postgres `traces` table | Same hook pattern as `AgenticCRE`'s bridge but with one sink instead of a JSONL recorder. Cost, latency, tokens, tool-call names captured automatically. |

## Data layer

- **Ingestion:** a one-shot `ingest` service in docker-compose. Streams each CSV via `psycopg.copy()` (binary COPY is dramatically faster than INSERT loops). Idempotent — safe to re-run; uses `INSERT … ON CONFLICT DO NOTHING` against natural keys or a content-hash unique constraint where no natural key exists.
- **Schema (planned):**
  - `abc_headlines(publish_date DATE, headline TEXT)` — Australian general news.
  - `us_headlines(headline_id BIGSERIAL, source TEXT, published_at TIMESTAMPTZ, headline TEXT, ticker TEXT)` — both US sources unioned with a `source` discriminator (`'analyst' | 'partner'`).
  - `securities(ticker TEXT PRIMARY KEY, security_name, sector, sub_industry, address, date_first_added DATE, cik)`.
  - `prices(date DATE, ticker TEXT, open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC, volume BIGINT, PRIMARY KEY (ticker, date))`.
  - `fundamentals(ticker TEXT, period_end DATE, …wide metric columns…)`.
  - `traces(id BIGSERIAL, task_id UUID, turn_index INT, ts TIMESTAMPTZ DEFAULT now(), model TEXT, role TEXT, prompt JSONB, response JSONB, tool_calls JSONB, input_tokens INT, output_tokens INT, cost_usd NUMERIC, latency_ms INT, error TEXT)`.
- **Indexes:**
  - GIN over `to_tsvector('english', headline)` on `abc_headlines` and `us_headlines`.
  - B-tree on `published_at` and `ticker` for `us_headlines`.
  - B-tree on `(ticker, date)` for `prices` (covered by PK).
- **pgbouncer:** transaction-pooling mode. The agent service connects to `pgbouncer:6432`. Note: transaction-pooling disables prepared statements, so `asyncpg` is configured with `statement_cache_size=0`.

## Agent design

- **Loop:** LiteLLM tool-use with a tool-use round budget (`max_rounds_with_tool_calls`, default 10), per-tool timeout, and compaction triggered above a configurable cumulative-input-token threshold (default 20k). When the budget is exhausted with tool calls still pending, the model never got a turn to consume the last tool result — so one final tool-free `acompletion` call lets it synthesize an answer from the history. A capped run therefore still ends with a real answer, not a stub, at a worst-case cost of budget + 1 LLM calls.
- **Tool model — load then analyze.** Tools split in three kinds. *Inline tools* (`headline_search`, `headline_topic_frequency`, `lookup_security`) return small results directly. The *loader* (`load_prices`) pulls a potentially large result set into a server-side pandas DataFrame held in the run's `DatasetRegistry` and returns only a **handle** plus the frame's shape, schema, and a tiny head sample. *Analysis tools* (`dataset_describe`, `dataset_sample`, `dataset_rolling`, `dataset_correlation`, `dataset_arima`) compute over a handle and return small results. The agent never receives bulk data rows.
- **Why this shape.** A single multi-year `price_history` result was ~47k tokens of raw rows fed straight into context. Truncating loses data the agent may need; conversation compaction cannot help — it summarizes *old* turns, not a fresh oversized result. The load-then-analyze split eliminates large tool results *by construction*: raw rows never leave the server, so there is no oversized result to cap. Compaction is left to do only its real job (history accumulation).
- **Dataset lifecycle.** One `DatasetRegistry` per run, cleared in a `finally` when the run ends. In-process memory; no TTL or eviction because the lifetime is bounded to a single task. Loaders apply a generous RAM row cap (`AGENT_DATASET_MAX_ROWS`, default 200k) — a memory guard, distinct from the (eliminated) context-size problem.
- **Fixed analysis menu = deliberate capability boundary.** The `dataset_*` tools are the complete supported operation set. Requests outside it (fit an LSTM, train XGBoost, bespoke econometrics) have no tool; the system prompt instructs the agent to decline rather than improvise. ARIMA is retained as one analysis op, honestly framed as a naive baseline.
- **Misapplication guard.** The advertised tool list is static (low complexity). Rather than dynamically varying it per dataset type, each analysis tool validates the handle's schema (column exists, dtype numeric where required) and returns a clear error the agent recovers from — cheaper and more robust than maintaining a dataset-type→tool mapping.
- **Safety.** Every tool is a hardcoded, reviewed query — no path exists for model-authored SQL to reach Postgres (see the removal note below). The loader caps rows pulled into memory (`AGENT_DATASET_MAX_ROWS`); every tool input is logged to `traces` before execution.
- **Removed: the `load_dataset_sql` SQL escape hatch.** An earlier iteration shipped a `load_dataset_sql` tool that ran model-authored read-only SQL (`SELECT`/`WITH` only, read-only role, `statement_timeout`, outer `LIMIT`) so the agent could express joins and aggregations the structured loader doesn't cover. It was removed deliberately, for three reasons. (1) **It contradicted the capability boundary.** The whole design principle is a *fixed* analysis menu the agent cannot exceed; an arbitrary-SQL tool is an unbounded capability bolted onto exactly that boundary. (2) **It was an architectural liability.** Its read-only pool connected to Postgres *directly*, bypassing pgbouncer — necessary so a per-connection `statement_timeout` would hold under transaction pooling, but it forgoes pgbouncer's connection capping, so N agent replicas would open N×(pool size) direct connections and exhaust `max_connections` at scale. (3) **What it actually enabled was thin.** The headline use case — "correlate news with prices" — reduces, with this toolset, to correlating daily *headline counts* against returns. Count measures attention, not sentiment; a directional news↔price signal needs a per-headline **sentiment score**, i.e. an LLM/NLP scoring pass at ingestion or as a dedicated tool. That is a real feature, deliberately out of scope here (it is several hundred lines, with its own compaction and cost considerations). Removing the escape hatch leaves one connection path, all through pgbouncer, and a capability boundary that is now genuinely fixed. The proper at-scale reintroduction is sketched under "How we'd evolve" below.

## Observability

- Each agent turn writes a row to `traces`: task_id, turn_index, model, prompt JSONB, response JSONB, tool_calls JSONB, token counts, cost, latency, error.
- The LiteLLM telemetry callback feeds the same table — every LLM call captured uniformly regardless of where it originates.
- **`GET /traces/{task_id}` makes the telemetry observable** — it replays a task as an ordered timeline of every LLM call and tool call (`?verbose=true` adds full prompts/responses/tool I/O). `python agent.py … --trace` prints that timeline after a run; `--trace-only <id>` inspects a past run. Collecting traces with no way to read them is theatre; this closes the loop. The endpoint is read-only and unauthenticated for the demo — in production it sits behind the same auth as `/run`.
- **Extended thinking is enabled** (`AGENT_EXTENDED_THINKING`, with the interleaved-thinking beta). The model's reasoning is captured into `traces.response` and surfaced by the endpoint above, so a run can be audited for *why* the agent chose each tool, not just *what* it did. Thinking blocks are preserved across tool-result turns in `loop.py` (Anthropic rejects the conversation otherwise).
- Stdout logs are structured (JSON) so a future log aggregator can ingest them without parsing changes.

## Known limitations (deliberate)

- **ABC News is Australian; financial data is US.** Any news↔price analysis is only meaningful on the US headlines (`us_headlines`), not ABC News. ABC News is retained as a separate corpus to demonstrate multi-source tooling and honor the original brief.
- **No fuzzy/trigram matching (`pg_trgm`).** The LLM is the query author here, not a human typing into a search box. The agent can rephrase or normalize queries itself if `tsvector` returns nothing. `pg_trgm` would be the right call if the surface were a search UI for end users.
- **No semantic search / embeddings.** `tsvector` covers keyword recall. Documented as a next step but skipped to keep ingestion cheap and the demo crisp. `pgvector` is a schema-migration step away when wanted.
- **No MCP servers.** Considered as an extensibility surface; exposing in-process Python tools as MCPs is mostly ceremony for a single-agent demo, and no specific external capability emerged as load-bearing.
- **No web fetch.** Agent is grounded only in the local datasets.
- **Single-tenant, no auth.** FastAPI service is unauthenticated; assumes a trusted caller.
- **Dataset registry is in-process.** Loaded datasets live in the agent process's memory, scoped to one run. Fine for a single agent container; a multi-replica deployment would need a shared results store (see below).
- **No cross-dataset or ad-hoc SQL analysis.** The agent analyzes a price dataset (via `load_prices` + `dataset_*`) and the headline corpora (via `headline_*`) *separately*; there is no tool to join them or run arbitrary queries. For a "did news move the stock?" question the agent reports headline activity and price movement side by side rather than a single computed correlation. This is the consequence of removing `load_dataset_sql` (see "Removed: the `load_dataset_sql` SQL escape hatch" under Agent design); a meaningful directional news↔price capability needs sentiment scoring and is out of scope.
- **Fixed analysis menu.** The agent can only run the operations the `dataset_*` tools expose (summary stats, sampling, rolling stats, correlation, ARIMA). Custom modeling — LSTM, gradient-boosted trees, bespoke econometrics — is out of scope by design and declined rather than faked.
- **No Terraform.** Considered for the production-deployment story; rejected as adding noise without paying for itself in a take-home. The "deploy at scale" story lives in prose below.

## How we'd evolve this toward production at scale

- **Storage:** swap the in-container Postgres for **managed Postgres** (RDS / Cloud SQL) with read replicas behind pgbouncer. Agent code is unchanged — only the connection string. Vertical-scale the writer; horizontal-scale readers via the replica fan-out.
- **Ingestion:** move from a one-shot container → orchestrated DAG (Airflow / Prefect / Dagster) with backfills, schema validation, dead-letter handling, and incremental loads. Watermarks per source.
- **Embeddings:** add an embedding pass at ingestion + `pgvector` HNSW index. Hybrid retrieval (`tsvector` BM25 + cosine rerank). NER at ingestion so ABC News headlines can be linked to tickers where mentions exist.
- **Agent runtime:** the FastAPI service runs behind a load balancer, autoscales on inflight requests, and offloads long-running tasks (multi-minute ARIMA fits, heavy loads) into a worker pool with a queue (Redis / SQS / Kafka). HTTP boundary returns a `task_id` immediately; clients poll `/tasks/{id}` for status.
- **Observability:** OpenTelemetry traces end-to-end; Langfuse (or equivalent) for prompt/response review; alerting on latency, error rate, token cost, and tool-error rate per task class.
- **Ad-hoc query + cross-dataset analysis:** if open-ended querying is genuinely needed, it returns not as an in-process SQL tool but as a **dedicated query service** — row-level RBAC, query-plan inspection, per-tenant budgets, circuit-breaking on expensive plans, and its own connection pooling — so the agent never holds raw DB access. For the news↔price use case specifically, the right move is a **sentiment-scoring pass**: score each headline at ingestion (or via a bounded scoring tool), store a numeric `sentiment` column, and the existing `dataset_correlation` then operates on a real directional signal — no escape hatch required.
- **Dataset store:** the in-process `DatasetRegistry` becomes a shared results store (Redis / Arrow Flight / a small results service) so loaded datasets survive across workers and outlive a single request — enabling multi-replica agents and letting an out-of-band UI page a result without an LLM round-trip. The analysis tools are unchanged; only `get`/`put` move behind the network.
- **Eval harness:** task fixtures with expected outputs; regression tests on prompt or model changes; offline scoring before any model swap.
- **Secrets:** vault-backed config (Vault / AWS Secrets Manager / Doppler); per-tenant API key scoping; automatic rotation.
- **MCP:** when extensibility is actually needed, expose stable, vetted tools as MCP servers so other agents in the org can compose them. Adopting MCP only once we have a reuse story makes the abstraction earn its weight.

## Open items

- Exact compaction trigger thresholds for our (likely shorter) conversations — tune empirically.
- Initial agent system prompt and seed examples.
- Eval fixture set (golden tasks + expected behavior).
- ~~Whether to provide a single composite tool vs. primitives.~~ Resolved: primitives — loaders + a fixed analysis menu the agent composes (load → describe → sample/rolling/correlate/arima).
