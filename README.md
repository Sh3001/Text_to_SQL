# Query Warden

A natural-language analytics chatbot for a live PostgreSQL warehouse, built
so that the safety guarantee does not depend on the language model
behaving. Full architecture, phased build plan, and rationale:
see the design doc (published separately as an Artifact).

```mermaid
flowchart LR
    Q[Question] --> G[Generate<br/>Ollama, qwen2.5-coder:3b]
    G -->|repair, 2x max| G
    G --> AST{AST Guard<br/>pglast}
    AST -->|unsafe, terminal| BLOCK[["Blocked"]]
    AST -->|repairable| G
    AST -->|ok| BUDGET{Plan Budget<br/>EXPLAIN}
    BUDGET -->|over budget| G
    BUDGET -->|ok| CONF{Confidence}
    CONF -->|high| EXEC[Execute<br/>chatbot_ro, read-only]
    CONF -->|medium / low| GATE[/Approval Gate<br/>human reviews or edits SQL/]
    GATE -->|approve| AST
    GATE -->|reject| DISCARD[Discarded]
    EXEC --> ZERO{0 rows?}
    ZERO -->|yes| DIAG[Diagnose<br/>which filter emptied it]
    ZERO -->|no| RESULT[Result + chart]
    EXEC -.-> AUDIT[(audit.query_log<br/>chatbot_ro: no access)]
    BLOCK -.-> AUDIT
```

Edited SQL from the approval gate re-enters the guard, same as model
output — the diagram's only shortcut is drawing that as one arrow back
to `AST Guard` rather than two identical paths.

## Status

**Phases 00 through 07 are complete and verified against a live database, a
live model, and a real browser — not just written.**

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
- `api/app/pipeline/plan_budget.py` — layer 3 of the defense stack (EXPLAIN
  before execute), missing after Phase 03 and added in Phase 04 — read-only
  isn't the same as safe.
- `api/app/pipeline/diagnose.py` — the zero-row selectivity diagnostic:
  re-runs a query's WHERE-clause predicates cumulatively (built from the
  AST, not string surgery) and names the exact one that emptied the result.
- `api/app/pipeline/answer.py` + `errors.py` — the full error taxonomy from
  the design doc and the bounded (two-attempt) repair loop: every failure
  is classified once and handled by exactly the action its class calls for
  — ask, repair, block, diagnose, or give up.
- `eval/` — the evaluation harness: 52 golden pairs, 30 ambiguity cases,
  and the adversarial CI gate, graded by real execution accuracy
  (`api/app/pipeline/grading.py`), not string comparison. One command:
  `eval/run_eval.py`.
- `api/app/main.py` + `api/app/api/` — the FastAPI app: two SSE
  endpoints (`POST /api/query`, `POST /api/query/approve`) streaming
  real progress events from the pipeline's own `on_event` callback
  (`answer.py`'s `plan()`/`finish()` split — Phase 04's single blocking
  `answer()` call refactored into two composable phases so execution can
  genuinely pause for approval, not simulate pausing).
- `web/` — the chat UI: React + TypeScript + Vite, no framework beyond
  that. One `QueryCard` component renders every state a turn can be in;
  a chart renders only after validating the model's proposed axes
  against the real result columns, falling back to the table otherwise.
- `db/03_observability.sql` + `api/app/obs/` — `audit.query_log`, written
  only by the app's own trusted connection (`chatbot_ro` has zero grants
  on the `audit` schema — reserved since Phase 00, wired up here). One
  row per terminal outcome, blocked attempts included; `GET /api/stats`
  and `GET /api/audit` read it back for the dashboard and the audit view.
- `web/src/components/Activity.tsx` — the second tab in the UI: query
  volume and latency by verdict, and every blocked/given-up attempt with
  its timestamp, question, and the model's actual SQL.
- 238 tests total: guard/schema tests run with no DB and no model; live-DB
  and live-model integration tests skip cleanly when either is unavailable.
  Every row of the error taxonomy has a test that provokes it and asserts
  the recovery (`api/tests/test_answer_repair_loop.py`); the approval
  gate's full pause/approve/reject/re-guard-edited-SQL flow is proven in
  `api/tests/test_api.py`; `chatbot_ro`'s zero access to the audit schema
  is a real, executable regression test now
  (`api/tests/test_audit.py::test_chatbot_ro_has_zero_access_to_the_audit_schema`),
  not just something checked once by hand at a `psql` prompt.

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
mistake).

### Phase 04 checkpoint — same 20 questions, now with the repair loop

Re-run against the same file after Phase 04 landed: **19 answered, 1
correctly asked for clarification, 0 blocked, 0 failed, 0 gave up.** Both
Phase 03 failures were the exact two questions that needed the repair
loop — each took its full 2 attempts and both recovered, with the real
Postgres/guard error fed back verbatim rather than a generic "try again".

**The honest part.** While re-verifying one of those two recoveries
("What is our net revenue by region?"), the model's second repair attempt
produced SQL that passed the guard, passed the plan-budget check, and
executed cleanly with plausible-looking non-zero numbers — and was wrong.
It joined `v_order_items` and `v_refunds` directly to the same `v_orders`
row instead of pre-aggregating each first, which is exactly the fan-out
double-counting bug fixed in `semantic/catalog.yml` during Phase 03, just
reintroduced through a different SQL shape the earlier fix didn't cover.
Checked against a known-correct pre-aggregated version of the same
question: `total_refunds` came out roughly **3x too high** in every
region. Nothing in the pipeline caught it, because nothing *can* — nothing
about that query is unsafe, slow, or malformed; it's just semantically
wrong in a way only a human (or a golden-answer eval) can catch. That's
exactly the "valid but wrong" row in the error taxonomy below, which the
design doc already says isn't automatically detectable — this is what
that sentence means in practice, not a hypothetical. The one mitigation
that's actually in scope for this phase — a general hard rule in the
system prompt against joining more than one many-to-one child table
directly and summing across it, rather than just the one example that was
there before — was added and re-verified: three follow-up runs of the
same question all correctly pre-aggregated in a subquery afterward. A
prompt change reduces how often this happens; it does not make the class
of bug impossible, and nothing in this architecture claims otherwise.
Phase 05's golden-answer eval harness is where this stops being "probably
fixed" and becomes a measured number.

| Failure | Action | Test |
|---|---|---|
| Ambiguous question | ask | `test_ambiguous_question_asks_instead_of_guessing` |
| Unknown table/column | repair | `test_unknown_table_is_repaired_with_the_real_error_fed_back` |
| Syntax error | repair | `test_guard_level_syntax_error_is_repaired` |
| Unsafe statement | block, terminal | `test_unsafe_statement_is_blocked_not_repaired` |
| Over plan budget | repair | `test_over_budget_query_is_repaired` |
| Timeout | ask, never retried | `test_timeout_asks_and_does_not_retry` |
| Zero rows | diagnose | `test_zero_rows_triggers_diagnosis_naming_the_culprit_predicate` |
| Model unavailable | give up, no retry | `test_model_unavailable_gives_up_without_retrying` |
| Model output unparseable | repair | `test_unparseable_model_output_is_repaired` |
| Valid but wrong | *(not automatically detectable — see above)* | — |

### Phase 05 — the eval harness, and three real bugs it found before grading a single model answer

`eval/` ships 52 individually-verified golden pairs (20 easy / 20 medium /
12 hard — scoped down from the plan's 150, deliberately: see
`eval/README.md` for why 52 verified beats 150 unverified), 30 ambiguity
cases (15 genuinely ambiguous, 15 clear-but-tricky), and the adversarial
suite reused directly from Phase 01 rather than duplicated. Run it with
`cd eval && PYTHONPATH=../api ../.venv/bin/python run_eval.py`.

Building it surfaced three real, previously-undetected bugs — not in the
model, in this project's own code, each caught because a golden case's
real result didn't match what a careful read of the schema said it
should:

1. **A data bug three phases old.** `avg_rating_by_category` returned 1
   row instead of ~15. `db/01_seed.sql`'s product-seeding query had the
   exact uncorrelated-random-in-a-subquery bug documented at that file's
   own top (the one that broke order statuses in Phase 00) — in a spot
   the original verification pass never specifically checked. All 2000
   products had silently landed in a single category since the first
   commit. Fixed at the source; every category-based query in this
   project was wrong until this phase.
2. **A real bug in the guard.** `WITH order_totals AS (...) SELECT ...
   FROM order_totals` was rejected as `UNKNOWN_TABLE` — the catalog check
   had no notion of a query's own CTE names, so it treated a locally
   -defined CTE as a hallucinated table. This would have blocked any
   correctly-formed multi-CTE query from any model, Claude included, not
   just this project's — a real regression sitting quietly since Phase 02.
   Fixed by collecting CTE names before the catalog walk and exempting
   them.
3. **A bug in the eval harness's own grading logic.** Several golden
   cases were graded wrong even when the candidate's rows were
   byte-identical to gold once sorted. The grader treated "gold SQL has
   an `ORDER BY`" as "row order is part of the correct answer" — but
   `ORDER BY` on a plain GROUP-BY breakdown (no `LIMIT`) was only added
   for the golden fixture's own readability, and Postgres's hash
   aggregate returns two independently-run, semantically-identical
   queries in different physical row orders. 20 of the 52 golden cases
   (nearly 40%) hit this. Fixed: order only counts as part of the answer
   when `ORDER BY` is paired with `LIMIT` — a real top-N shape, where a
   different sort genuinely changes which rows survive — not `ORDER BY`
   alone.

**Results**, run after all three fixes — the first genuinely trustworthy
numbers this project has, and the honest baseline every later change
gets measured against:

| Golden set | Execution accuracy | Valid-SQL rate |
|---|---|---|
| Easy (20) | **95%** (19/20) | 100% |
| Medium (20) | **70%** (14/20) | 100% |
| Hard (12) | **25%** (3/12) | 58% |
| **Overall (52)** | **69%** (36/52) | 90% |

Avg latency 10.5s, p95 35.7s, cost $0.00 (local model). Ambiguity set:
53% overall, but the two halves tell the real story — **7% recall**
(asked when it genuinely should have) against **0% false-positive rate**
(never asked needlessly). That's a 3B local model's honest character on
this task: extremely reluctant to say "I don't know," happy to guess
confidently on a question a larger model would flag — exactly the
overconfidence this suite exists to measure, not a harness artifact.
Adversarial: 61/61 blocked, 0 leaked, gate passes.

The 16 golden misses read as genuine model limitations, each checked by
hand, not further harness bugs: an unwarranted extra filter conflating
"tickets *opened* in the last 6 months" with ticket `status = 'open'`
(confirmed by inspecting the actual generated SQL); an average computed
over the wrong grain (line items instead of one total per order — the
exact double-weighting trap `catalog.yml`'s own metric note warns
about); two more CTE-truncation failures beyond the one already covered
in Phase 04, correctly given up on after exhausting the repair budget
rather than executing something broken. The accuracy cliff between easy
(95%) and hard (25%) is the honest shape of a 3B model on a real schema:
strong on single-table lookups, weak on multi-CTE joins with a
canonical-but-nontrivial metric definition to reproduce — which is
exactly the profile Phase 06's UI needs to design around (surface
confidence, make the SQL editable, don't hide the hard-tier failure
rate behind a confident-sounding chat bubble).

### Phase 06 checkpoint — driven in a real headless browser, not just curled

"A stranger can ask a question, read the assumptions, approve the SQL,
and get a chart" — the design doc's own Phase 06 checkpoint, verified
with Playwright against the actual running app (`npm run dev` + `uvicorn`),
not just the API in isolation. Two real bugs surfaced by actually looking
at what rendered, not by assuming a passing type-check meant a correct
page:

1. **A React anti-pattern that would have silently eaten every SQL
   edit.** The approval-gate textarea's `onChange` handler originally
   mutated `turn.editedSql` directly on the prop object — React never
   re-renders from that, so every keystroke in the "edit before you
   approve" box would have been invisible on screen while silently not
   updating the state actually sent to `/approve`. Caught before ever
   loading the page, by re-reading the component; fixed by lifting the
   edit into a proper `onEditSql` callback prop.
2. **An unreadable Y-axis on every currency chart.** Recharts' default
   tick formatting collided into a stack of literal `"0000000"` labels
   on this project's revenue figures (hundreds of millions) — a real
   rendering bug, only visible in an actual screenshot; `tsc` and the
   component's own logic gave no signal anything was wrong. Fixed with
   `Intl.NumberFormat`'s compact notation (`140M`) and a wider axis.

The live model rarely produces anything below `"high"` confidence (see
`web/README.md`), so the approval-gate pause was exercised live via a
deliberately-tricky question and directly via `test_api.py`'s stubbed
-model tests, not assumed working from the code alone.

### Phase 07 checkpoint — the dashboard, seeded with real traffic

"The README's headline is a measured number, not an adjective" — the
design doc's own Phase 07 checkpoint. It's satisfied twice over: the
Phase 05 section above leads with 95%/70%/25% execution accuracy, not
"the model is pretty accurate"; and the Activity tab now shows the same
discipline live, reading real rows out of `audit.query_log` rather than
a static mock. Verified the same way as Phase 06 — a real browser, real
questions asked through the actual running app, one representative
blocked attempt seeded in for the audit view — not assumed correct from
the endpoint code alone: 3 questions asked, 87ms avg latency, 1 blocked,
the verdict-breakdown table matching exactly, and the blocked attempt
showing its real generated SQL and rejection reason in the audit list.

**What's still a named gap, not an oversight:** the design doc's
`conversations` and `messages` tables (multi-turn history surviving a
restart) and the pending-plan approval state (`app.state.pending_plans`,
still in-memory — Phase 06's gap, unchanged by this phase) are both
still missing; only the *terminal outcome* of a question is durable.
And the design doc's other Phase 07 deliverable — "a 60-second demo
GIF" — is a real screen recording a human needs to make; nothing in
this environment can produce one, so it's named here rather than
silently dropped or faked.

## Quickstart

### Option A — Docker Compose (the documented path)

```bash
docker compose up -d postgres
# waits on the healthcheck, then seeds itself from db/*.sql automatically
```

### Option B — a Postgres already running locally

```bash
./db/reset.sh            # drops/recreates `querywarden`, applies all four SQL files
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
psql -U chatbot_ro -h localhost -d querywarden -c "SELECT * FROM audit.query_log"
#   -> ERROR: permission denied for schema audit
```

### Run the test suite

```bash
python3 -m venv .venv && .venv/bin/pip install -r api/requirements.txt
cd api && ../.venv/bin/python -m pytest -v
# 238 passed. Guard/schema-fixture tests always run; live-DB and
# live-Ollama integration tests skip cleanly if either isn't reachable.
```

### Run the pipeline yourself

```bash
ollama pull qwen2.5-coder:3b   # if not already pulled
cd api && ../.venv/bin/python -m app.pipeline.cli "How many orders were shipped?"
# or run the full Phase 03 checkpoint:
../.venv/bin/python -m app.pipeline.cli --file ../eval/phase03_questions.txt
```

### Run the eval harness

```bash
cd eval && PYTHONPATH=../api ../.venv/bin/python run_eval.py
# or just the fast adversarial CI gate, no live model calls:
PYTHONPATH=../api ../.venv/bin/python run_eval.py --adversarial-only
```

### Run the full app — API + web UI

```bash
# terminal 1
cd api && DATABASE_URL=postgresql://postgres:postgres@localhost:5432/querywarden \
  ../.venv/bin/python -m uvicorn app.main:app --port 8001

# terminal 2
cd web && npm install && cp .env.example .env && npm run dev
# open http://localhost:5173 — the "Activity" tab reads real rows back
# out of audit.query_log once you've asked a few questions.
```

## A note on the seed script

`db/01_seed.sql` documents a real Postgres planner gotcha at its top: a
volatile expression (`random()`, `now()`) inside a `LATERAL` subquery or a
`WHERE` clause can be evaluated **once** and reused across every output row,
rather than per row, whenever the expression doesn't reference an actual
column from the "many rows" side of the join. It bit this project four
separate times, not three: the order-status/currency bug found during
Phase 00 (documented there), and a fourth instance — every one of 2000
products landing in a single category — that sat undetected through
Phases 00 through 04 and was only found in Phase 05, by a golden-set query
whose result looked obviously wrong. The fix is always the same: compute
per-row randomness directly in a CTE's own top-level `SELECT` list, driven
off a real row source, never inside a subquery lacking a genuine
correlated column reference.

## Next

The design doc's full phased build plan (00 through 07) is complete —
this is the first point in the project where "Next" isn't the next
numbered phase. What's left is scoped in the design doc's own "What to
cut, and what never to cut" section and in the gaps named throughout
this README, the largest being: `conversations`/`messages` persistence
(multi-turn history surviving a restart), the pending-plan approval
state moving from `app.state.pending_plans` into Postgres, and a real
150-case golden set (this project ships 52, individually verified —
see `eval/README.md` for why that trade was made deliberately). None of
these are surprises; each was named honestly in the phase where it was
deferred, not discovered now.
