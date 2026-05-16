# Architecture (diagrams)

Visual companion to [WALKTHROUGH.md](WALKTHROUGH.md) (prose) and
[DECISIONS.md](DECISIONS.md) (rationale). All diagrams are Mermaid —
they render on GitHub and in most Markdown previewers.

---

## 1. Container topology

What runs where, and how the pieces connect, after `docker compose up`.

```mermaid
graph TB
    subgraph HOST["host machine"]
        CLI["agent.py<br/>CLI client (stdlib only)"]
        CSV[("6 CSV files<br/>~650 MB")]
    end

    subgraph STACK["docker compose — project: anthelion_take_home"]
        INGEST["ingest<br/>one-shot, idempotent"]
        AGENT["agent<br/>FastAPI + uvicorn :8000"]
        PGB["pgbouncer :6432<br/>transaction pooling"]
        PG[("postgres 16<br/>tables + GIN FTS + traces")]
    end

    CLI -->|"POST /run {task}"| AGENT
    CSV -.->|"streamed COPY"| INGEST
    INGEST -->|"bulk load"| PG
    AGENT -->|"writer pool"| PGB
    PGB --> PG
    AGENT -->|"reader pool — read-only role, direct"| PG
```

- **ingest** runs once, populates Postgres, exits. The agent service
  waits for it via `depends_on: service_completed_successfully`.
- The **writer pool** goes through pgbouncer (the hot path — production
  shape). The **reader pool** (used only by the `sql()` tool) connects
  directly as a read-only role with a statement timeout.

---

## 2. Request lifecycle — the agent loop

What happens for one `python agent.py "..."` invocation.

```mermaid
sequenceDiagram
    participant U as agent.py (CLI)
    participant F as FastAPI /run
    participant L as agent loop
    participant C as compaction
    participant LLM as LiteLLM → Claude
    participant T as tools
    participant DB as Postgres

    U->>F: POST {task}
    F->>L: run_agent(task)

    loop until final answer OR round budget exhausted
        L->>C: should_compact(messages, tokens)?
        C-->>L: maybe compacted history
        L->>LLM: acompletion(messages, TOOL_DEFINITIONS)
        LLM-->>L: assistant message
        Note over LLM,DB: telemetry callback → 'llm_call' trace row

        alt message has tool_calls
            loop each tool call
                L->>T: dispatch(name, args)
                T->>DB: SQL query (FTS / prices / ...)
                DB-->>T: rows
                T-->>L: JSON result
                Note over L,DB: 'tool_call' trace row written
            end
        else no tool_calls
            L-->>F: AgentResult(answer, turns, tokens, cost)
        end
    end

    F-->>U: {answer, turns, tokens, cost_usd}
```

The loop is the core: call the model, run any tools it asked for, feed
results back, repeat. It ends when the model replies with no tool calls
(it has an answer); if the round budget runs out first, a final
tool-free call (diagram 3) synthesizes the answer from the history.

---

## 3. Agent loop — decision flow

The same loop as a flowchart, focused on the branch logic.

```mermaid
flowchart TD
    START([task arrives]) --> SEED["messages = [system prompt, user task]"]
    SEED --> CHECK{"cumulative tokens<br/>over threshold?"}
    CHECK -->|yes| COMPACT["compact: summarize old turns,<br/>keep recent tail verbatim,<br/>walk back for tool-pair safety"]
    CHECK -->|no| CALL
    COMPACT --> CALL["LiteLLM acompletion<br/>(messages + 8 tools)"]
    CALL --> TOOLS{"model returned<br/>tool calls?"}
    TOOLS -->|no| DONE([return answer + run summary])
    TOOLS -->|yes| DISPATCH["dispatch each tool,<br/>append results as tool messages,<br/>write tool_call traces"]
    DISPATCH --> CAP{"round budget<br/>exhausted?"}
    CAP -->|no| CHECK
    CAP -->|yes| SYNTH["final tool-free acompletion<br/>(synthesize answer from history)"]
    SYNTH --> DONE
```

---

## 4. Tools → data

The 8 tools the model can call, and the tables they read.

```mermaid
graph LR
    subgraph TOOLS["tool registry (service/tools.py)"]
        T1["headline_search"]
        T2["headline_topic_frequency"]
        T3["lookup_security"]
        T4["price_history"]
        T5["returns"]
        T6["forecast_returns"]
        T7["fundamentals_lookup"]
        T8["sql — read-only escape hatch"]
    end

    ABC[("abc_headlines")]
    US[("us_headlines")]
    SEC[("securities")]
    PR[("prices")]
    FUN[("fundamentals")]

    T1 --> ABC
    T1 --> US
    T2 --> ABC
    T2 --> US
    T3 --> SEC
    T4 --> PR
    T5 --> PR
    T6 --> PR
    T7 --> FUN
    T8 -.->|"any table, SELECT only"| ABC
    T8 -.-> US
    T8 -.-> SEC
    T8 -.-> PR
    T8 -.-> FUN
```

The first 7 are structured, scoped tools. `sql()` is a guarded escape
hatch (read-only role, statement timeout, row cap) for anything the
structured tools don't cover.

---

## 5. Data model

```mermaid
erDiagram
    securities {
        text ticker PK
        text security_name
        text sector
        text sub_industry
    }
    prices {
        text ticker
        date date
        numeric close
        bigint volume
    }
    fundamentals {
        text ticker
        date period_end
        jsonb data
    }
    us_headlines {
        text source
        timestamptz published_at
        text headline
        text ticker
        tsvector headline_tsv
    }
    abc_headlines {
        date publish_date
        text headline
        tsvector headline_tsv
    }
    traces {
        uuid task_id
        int turn_index
        text kind
        jsonb prompt
        numeric cost_usd
    }

    securities ||--o{ prices : "ticker"
    securities ||--o{ fundamentals : "ticker"
    securities ||--o{ us_headlines : "ticker (soft link)"
```

- `abc_headlines` stands alone — Australian general news, no ticker.
- `us_headlines.ticker` is a *soft* link to `securities` (no FK; the
  upstream data isn't clean enough to enforce one).
- `traces` is the observability sink — one row per LLM call or tool
  call, written by the telemetry callback and the agent loop.
