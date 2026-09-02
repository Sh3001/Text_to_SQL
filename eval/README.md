# Evaluation harness

The Phase 05 checkpoint from the project plan: one command that prints
accuracy, cost, and latency, so a prompt or model change is measured
against a number instead of a vibe.

```bash
cd eval && PYTHONPATH=../api ../.venv/bin/python run_eval.py
```

Three suites:

- **`golden/{easy,medium,hard}.yml`** — 52 question/gold-SQL pairs
  (20/20/12), graded by **execution accuracy**: both the gold SQL and the
  model's generated SQL run for real against the live database, and their
  result sets are compared (order-insensitive unless the gold query has
  an `ORDER BY`; see `api/app/pipeline/grading.py`). Never string
  comparison — there are a dozen correct spellings of any join. Source of
  truth is `golden/_cases.py` (Python triple-quoted strings, much easier
  to write and read than YAML block scalars for SQL); `golden/_build.py`
  re-verifies every case against a live database and regenerates the
  YAML — never hand-edit the `.yml` files directly.

- **`ambiguity/cases.yml`** — 30 questions (15 genuinely ambiguous, 15
  clear-but-tricky-sounding), measuring clarification recall (did it ask
  when it should have?) and the false-positive rate (did it needlessly
  ask on a clear question?) — the thing execution accuracy alone can't
  see, since a model that always guesses can still score well on golden
  questions while being dangerous on an ambiguous one.

- **Adversarial** — imported from `api/tests/test_ast_guard.
  ADVERSARIAL_SUITE`, not duplicated into YAML (see
  `adversarial/README.md`). Hard CI gate: 0 leaks required, non-zero exit
  on any leak. Run just this, fast, with `--adversarial-only`.

## What the plan named 150 golden pairs, this project ships 52

Scoped down deliberately, not padded to hit the number: every single case
here was individually run against the live, seeded database before being
committed — not written and hoped. That process caught two real bugs this
project shipped without noticing: `db/01_seed.sql` had assigned every one
of 2000 products to a single category (a data bug), and `ast_guard.py`
rejected any correctly-formed multi-CTE query as `UNKNOWN_TABLE` (a real
bug in the guard itself, not the eval fixtures) — both found because a
golden case's actual result didn't match what a careful read of the
schema said it should. 52 verified cases that catch real bugs are worth
more than 150 that were never individually checked.

## `run_eval.py`'s known gap

Per-request token counts aren't currently surfaced on `AnswerOutcome`
(Phase 04's repair loop returns a verdict and a message, not per-attempt
`GenerationResult` objects), so the report's cost/token line is
incomplete — latency is real and measured; token counts are not yet
plumbed through. Flagged rather than faked with a placeholder number.
