# Query Warden

Ask a PostgreSQL warehouse questions in plain English. A local LLM writes
the SQL; it never gets to run it.

```mermaid
flowchart LR
    Q[Question] --> G[Generate]
    G -->|repair, 2x max| G
    G --> AST{AST guard}
    AST -->|unsafe| BLOCK[["Blocked"]]
    AST --> BUDGET{Plan budget}
    BUDGET --> CONF{Confidence}
    CONF -->|high| EXEC[Execute as chatbot_ro]
    CONF -->|medium / low| GATE[/Human approves or edits/]
    GATE --> AST
    EXEC --> RESULT[Result + chart]
```

## The safety model

Four controls, each independent of the others:

1. **The database role.** `chatbot_ro` has no write privileges, no access
   to base tables (views only), and no access to the `audit` schema. It
   times out after 15s. Postgres enforces this, not application code.
2. **The AST guard.** Generated SQL is parsed with `pglast` (libpg_query,
   the real Postgres parser), not scanned for keywords. Only a single
   bare `SELECT` survives. The SQL that runs is re-deparsed from the
   validated tree, so the model's own string never executes.
3. **The plan budget.** `EXPLAIN` runs before execution. Read-only SQL
   can still be expensive: an aggregate over a cross join costs hundreds
   of millions regardless of any `LIMIT`.
4. **The approval gate.** Anything below `high` confidence pauses for a
   human. Edited SQL goes back through the guard.

Multi-tenant filtering lives in the view definitions and reads
`current_setting('app.tenant_id')`. When it isn't set, the views return
zero rows rather than everything.

## Quickstart

Needs Docker (or a local Postgres) and [Ollama](https://ollama.com).

```bash
ollama pull qwen2.5-coder:3b
docker compose up -d postgres      # seeds itself from db/*.sql on first boot

python3 -m venv .venv && .venv/bin/pip install -r api/requirements.txt
cd api && ../.venv/bin/python -m app.pipeline.cli "How many orders were shipped?"
```

Already have Postgres running locally? Use `./db/reset.sh` instead of
compose. It drops and recreates the `querywarden` database.

### Run the API and UI

```bash
# terminal 1
cd api && DATABASE_URL=postgresql://postgres:postgres@localhost:5432/querywarden \
  ../.venv/bin/python -m uvicorn app.main:app --port 8001

# terminal 2
cd web && npm install && cp .env.example .env && npm run dev
```

Open http://localhost:5173. The Activity tab reads real rows out of
`audit.query_log` once you've asked a few questions.

## Check the permissions hold

```bash
export PGPASSWORD=chatbot_ro
PSQL="psql -U chatbot_ro -h localhost -d querywarden -c"

$PSQL "DELETE FROM analytics.orders"                  # read-only transaction
$PSQL "SELECT * FROM analytics.legacy_orders_flat"    # permission denied
$PSQL "SELECT * FROM audit.query_log"                 # permission denied
$PSQL "SELECT count(*) FROM analytics.v_customers"    # 0, no tenant set
$PSQL "SET app.tenant_id='1'; SELECT count(*) FROM analytics.v_orders"
```

## Tests

```bash
cd api && ../.venv/bin/python -m pytest
```

238 tests. Guard and schema tests need neither a database nor a model.
Integration tests skip when Postgres or Ollama isn't reachable.

## Evaluation

Graded by execution accuracy: the golden SQL and the generated SQL both
run for real and their result sets are compared. See
[`eval/README.md`](eval/README.md).

```bash
cd eval && PYTHONPATH=../api ../.venv/bin/python run_eval.py
```

| Golden set  | Accuracy | Valid SQL |
| ----------- | -------- | --------- |
| Easy (20)   | 95%      | 100%      |
| Medium (20) | 70%      | 100%      |
| Hard (12)   | 25%      | 58%       |
| Overall     | 69%      | 90%       |

Average latency 10.5s, p95 35.7s. Adversarial suite: 61/61 blocked, which
gates the build. Ambiguity set: 7% clarification recall at a 0%
false-positive rate. The model rarely admits uncertainty, but it also
never asks needlessly.

The gap between easy and hard is the honest shape of a 3B model on a real
schema. It handles single-table lookups well and multi-CTE joins with a
canonical metric definition poorly. That's why the UI shows confidence
and makes the SQL editable.

## Layout

```
db/            schema, seed, roles, audit table
semantic/      business glossary and canonical metric SQL (hand-maintained)
api/app/
  guards/      the AST guard
  schema/      catalog introspection, DDL rendering, value index
  llm/         prompts, Ollama client, structured output schema
  pipeline/    generate, guard, budget, execute, diagnose, repair loop
  obs/         audit log writes and dashboard queries
  api/         FastAPI routes and the SSE bridge
web/           React chat UI
eval/          golden pairs, ambiguity cases, adversarial gate
```

`api/app/pipeline/errors.py` holds the error taxonomy: every failure the
pipeline can hit, mapped to one recovery action. Unsafe SQL is never
repaired, since feeding a blocked attempt back to the model is coaching
it. Timeouts are never retried.

## Why Ollama and not Claude

No Anthropic API key was available, so generation runs on a local
`qwen2.5-coder:3b` through JSON-schema-constrained decoding, which gives
the same structured-output guarantee. The hard-tier accuracy above is a
direct consequence. Swapping in a hosted model means rewriting
`api/app/llm/client.py` and nothing else.

## Known limitations

- **Wrong but valid SQL isn't detectable here.** A query can pass every
  check, run fast, and return plausible numbers that are wrong. There's a
  measured case where a fan-out join triple-counted refunds. Only a human
  or a golden answer catches that.
- **No authentication.** `tenant_id` currently comes from the request
  body, so the tenant isolation described above can be bypassed by
  sending a different number. Fixing this means deriving the tenant from
  a verified session.
- **Pending approvals are in-memory** (`app.state.pending_plans`), so
  they don't survive a restart and the API can't run more than one
  replica.
- **No conversation history.** Every question is independent.
- **The guard checks table names, not column names.** Unknown columns are
  caught by Postgres at execution and fixed by the repair loop.
- **Docker Compose only brings up Postgres.** The `api` and `web`
  services reference Dockerfiles that don't exist yet.

## More

- [`eval/README.md`](eval/README.md) — the three suites and how grading works
- [`web/README.md`](web/README.md) — UI structure and the approval gate
- [`db/01_seed.sql`](db/01_seed.sql) — a Postgres planner gotcha worth reading
  if you write seed scripts
