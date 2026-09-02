# Query Warden

A natural-language analytics chatbot for a live PostgreSQL warehouse, built
so that the safety guarantee does not depend on the language model
behaving. Full architecture, phased build plan, and rationale:
see the design doc (published separately as an Artifact).

## Status

**Phase 00 (foundation) and Phase 01 (the guard) are complete and verified
against a live database.** Everything below has actually been run, not just
written.

- `db/00_schema.sql` — the analytics schema: 14 base tables, 14 chatbot-facing
  views, and a deliberately excluded `legacy_orders_flat` trap table.
- `db/01_seed.sql` — deterministic seed (`setseed(0.42)`), ~200k orders /
  ~500k order_items, seeds in ~20s.
- `db/02_roles.sql` — the `chatbot_ro` role: no base-table access, no writes,
  15s statement timeout, per-tenant view-level filtering that fails closed.
- `api/app/guards/ast_guard.py` — the AST guard (layer 2 of the defense
  stack), built on `pglast`/`libpg_query`, the real Postgres parser.
- `api/tests/test_ast_guard.py` — 61 adversarial cases (all blocked), 11
  legitimate analytics queries (all approved), run with no DB and no API key.

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

### Run the guard test suite

```bash
python3 -m venv .venv && .venv/bin/pip install -r api/requirements.txt
cd api && ../.venv/bin/python -m pytest -v
# 124 passed — no database, no API key required
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

Phase 02 (schema intelligence: catalog introspection, the semantic layer,
the value index) and beyond are in the design doc.
