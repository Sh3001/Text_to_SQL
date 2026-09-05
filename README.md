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
zero rows rather than everything. That value comes from a signed claim in
the caller's token, which came from `app.users` at login — never from a
request field.

## Accounts

Every route except `/api/health` requires a bearer token. Two roles:
`member` asks questions, `operator` can also read the audit log and the
stats dashboard, and create accounts.

```bash
export JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
```

There is no default for `JWT_SECRET` and the API refuses to start without
it.

### Signing up

Sign up or sign in with an email address **or** a phone number. Accounts created
with a phone have no email at all, and vice versa — `app.users` requires
at least one of the two, not both.

Sign up from the app's own screen, or create accounts from the command
line:

```bash
cd api && ../.venv/bin/python -m app.auth.cli create-user you@example.com --role operator
cd api && ../.venv/bin/python -m app.auth.cli create-user "+91 98765 43210"
```

Phone numbers are normalised to E.164 before any lookup, so
`+91 98765 43210`, `+91-98765-43210` and `09876543210` all reach the same
account. A leading zero on a bare national number is treated as the trunk
prefix and dropped.

### Forgotten passwords

`Forgot your password?` on the sign-in screen prepares a reset. With no
mail or SMS provider wired up, an operator hands the link over:

```bash
cd api && ../.venv/bin/python -m app.auth.cli reset-password them@example.com
```

That prints a single-use link valid for an hour. Following it opens the
reset screen with the token filled in. Requesting a reset over HTTP always
answers the same way whether or not the account exists, so the route can't
be used to find out who has one; the CLI does say, because an operator
already knows.

Signed-in users can change their own password with `POST
/api/auth/password/change`, which requires the current one.

Both paths stamp `app.users.password_changed_at`, and every session token
pins that value — so a reset or a change signs out **every other session
immediately** rather than leaving a stolen one alive for the token's
remaining 12 hours. That costs one small query per authenticated request,
which is the strongest argument for adding the connection pool named
below.

One honest limit: signing up doesn't verify the address or number you
typed — there's no confirmation step, so you could register someone
else's. Closing that needs an email/SMS provider, which this project
deliberately doesn't ship.

The first account on a fresh instance becomes an operator, since somebody
has to be able to read the audit log. Everyone after that signs up as a
`member` in `DEFAULT_SIGNUP_TENANT_ID` (default 1). Role and tenant are
decided server-side and are not fields on the sign-up request, so
`/api/auth/signup` can't be used to escalate or to land in someone else's
tenant. Set `ALLOW_SIGNUP=false` to close sign-up and have operators
create accounts instead.

Conversations and their answers are stored in `app.conversations` and
`app.messages`. Reloading a thread replays the stored outcome rather than
re-running the SQL, so old answers don't silently change as the warehouse
does. `chatbot_ro` has no grants on the `app` schema at all, the same
posture as `audit`.

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
cd api && JWT_SECRET="$JWT_SECRET" \
  DATABASE_URL=postgresql://postgres:postgres@localhost:5432/querywarden \
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
$PSQL "SELECT * FROM app.users"                       # permission denied
$PSQL "SELECT count(*) FROM analytics.v_customers"    # 0, no tenant set
$PSQL "SET app.tenant_id='1'; SELECT count(*) FROM analytics.v_orders"
```

## Tests

```bash
cd api && ../.venv/bin/python -m pytest
```

335 tests. Guard and schema tests need neither a database nor a model.
Integration tests skip when Postgres or Ollama isn't reachable.
`test_auth.py`, `test_password_reset.py` and `test_history.py` cover the
auth boundaries — that a `tenant_id` in a request body is ignored, and
that a reset kills sessions issued in the same second it happens.

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
db/            schema, seed, roles, audit table, users, history, resets
semantic/      business glossary and canonical metric SQL (hand-maintained)
api/app/
  auth/        identifiers, password hashing, tokens, route dependencies
  history/     conversation + message persistence
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

## More

- [`eval/README.md`](eval/README.md) — the three suites and how grading works
- [`web/README.md`](web/README.md) — UI structure and the approval gate
- [`db/01_seed.sql`](db/01_seed.sql) — a Postgres planner gotcha worth reading
  if you write seed scripts
