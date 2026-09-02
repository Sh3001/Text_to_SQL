# Query Warden

A natural-language analytics chatbot for a live PostgreSQL warehouse, built
so that the safety guarantee does not depend on the language model
behaving. Full architecture, phased build plan, and rationale:
see the design doc (published separately as an Artifact).

## Status

**Phases 00 through 03 are complete and verified against a live database and
a live model — not just written.**

- `db/00_schema.sql` — the analytics schema: 14 base tables, 14 chatbot-facing
  views, and a deliberately excluded `legacy_orders_flat` trap table.
- `db/01_seed.sql` — deterministic seed (`setseed(0.42)`), ~200k orders /
  ~500k order_items, seeds in ~20s.
- `db/02_roles.sql` — the `chatbot_ro` role: no base-table access, no writes,
  15s statement timeout, per-tenant view-level filtering that fails closed.
- `api/app/guards/ast_guard.py` — the AST guard (layer 2 of the defense
  stack), built on `pglast`/`libpg_query`, the real Postgres parser.
- `api/app/schema/` — catalog introspection, deterministic DDL rendering,
  and the value index (typo-fuzzy matching + an explicit synonym map for
  semantic abbreviations like "California" → "CA", which fuzzy matching
  cannot do — see `value_index.py`'s module docstring).
- `semantic/catalog.yml` — the hand-maintained business glossary, canonical
  metric SQL, join paths, and deprecations.
- `api/app/llm/` + `api/app/pipeline/` — generation, guard, execution,
  wired end to end. **Runs on a local Ollama model, not Claude** — see
  "Why Ollama" below.
- 183 tests total: guard/schema tests run with no DB and no model; live-DB
  and live-model integration tests skip cleanly when either is unavailable.

### Why Ollama, not Claude

The design doc specifies Claude Opus 5 throughout. No Anthropic API key was
available in this environment (no paid account, and Anthropic has no
permanent free tier), and the user explicitly chose a free alternative over
signing up for one. Ollama was already installed locally with
`qwen2.5-coder:3b` pulled, so that's the generation backend actually wired
up: `api/app/llm/client.py` calls it via JSON-schema-constrained decoding
(verified directly against the running server, not assumed from docs — see
that file's docstring), which gets the same structured-output guarantee the
original plan wanted from Claude's `output_config.format`. The prompt-cache
story changes shape accordingly: Ollama has no billing, so there's nothing
to save money on, but llama.cpp reuses the KV-cache for a matching prompt
prefix within a warm model — measured at roughly 4x faster on a repeated
identical prefix (`api/app/llm/prompts.py`'s docstring) — so the same
discipline (frozen contract and schema first, volatile question last)
still pays off, just in latency instead of dollars. Swapping in Claude
later means rewriting `llm/client.py` only; nothing else in the pipeline
depends on Ollama's specific API shape.

### Phase 03 checkpoint — 20 hand-written questions

`eval/phase03_questions.txt`, run via
`python -m app.pipeline.cli --file ../eval/phase03_questions.txt`:
**17 executed successfully, 1 correctly asked for clarification** (a
genuinely ambiguous question — "How did we do last quarter?" — matching
the few-shot example's intent exactly), **1 correctly guard-blocked**
(the 3B model truncated a multi-CTE query and referenced a CTE it forgot
to define; the guard's catalog check caught the resulting unknown-table
reference and offered real "did you mean" suggestions rather than letting
broken SQL reach the database), and **1 failed at execution** (the model
queried `v_orders.amount`, which doesn't exist — a plain generation
mistake). Both non-clarification failures are exactly what Phase 04's
bounded repair loop exists to fix: feed the real Postgres/guard error back
for one retry. A caught-and-rejected mistake or a loudly failed query is
the correct outcome for a small local model's misfire — the alternative,
a confidently wrong number, is the one failure mode this whole project
exists to prevent, and it didn't happen once across the 20 questions.

## Quickstart

### Option A — Docker Compose (the documented path)

```bash
docker compose up -d postgres
# waits on the healthcheck, then seeds itself from db/*.sql automatically
```

### Option B — a Postgres already running locally

```bash
./db/reset.sh            # drops/recreates `querywarden`, applies all three SQL files
```

### Prove the floor holds

```bash
export PGPASSWORD=chatbot_ro
psql -U chatbot_ro -h localhost -d querywarden -c "DELETE FROM analytics.orders"
#   -> ERROR: cannot execute DELETE in a read-only transaction
psql -U chatbot_ro -h localhost -d querywarden -c "SELECT * FROM analytics.legacy_orders_flat"
#   -> ERROR: permission denied for table legacy_orders_flat
psql -U chatbot_ro -h localhost -d querywarden -c "SELECT count(*) FROM analytics.v_customers"
#   -> 0   (no app.tenant_id set: the view fails closed, not open)
psql -U chatbot_ro -h localhost -d querywarden -c "SET app.tenant_id='1'; SELECT count(*) FROM analytics.v_orders"
#   -> a real, tenant-scoped number
```

### Run the test suite

```bash
python3 -m venv .venv && .venv/bin/pip install -r api/requirements.txt
cd api && ../.venv/bin/python -m pytest -v
# 183 passed. Guard/schema-fixture tests always run; live-DB and
# live-Ollama integration tests skip cleanly if either isn't reachable.
```

### Run the pipeline yourself

```bash
ollama pull qwen2.5-coder:3b   # if not already pulled
cd api && ../.venv/bin/python -m app.pipeline.cli "How many orders were shipped?"
# or run the full Phase 03 checkpoint:
../.venv/bin/python -m app.pipeline.cli --file ../eval/phase03_questions.txt
```

## A note on the seed script

`db/01_seed.sql` documents a real Postgres planner gotcha at its top: a
volatile expression (`random()`, `now()`) inside a `LATERAL` subquery or a
`WHERE` clause can be evaluated **once** and reused across every output row,
rather than per row, whenever the expression doesn't reference an actual
column from the "many rows" side of the join — this happened three separate
times during development (every seeded order came back `status = 'refunded'`
the first time through) before landing on the fix documented there: compute
per-row randomness directly in a CTE's own top-level `SELECT` list, driven
off a real row source, never inside a subquery lacking a genuine correlated
column reference.

## Next

Phase 04 (error taxonomy, the bounded repair loop, zero-row diagnosis) and
beyond are in the design doc.
