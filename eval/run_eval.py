"""The Phase 05 checkpoint: one command that prints accuracy, cost, and
latency. Three suites, matching the project plan's evaluation section:

  * golden      (eval/golden/*.yml)     — execution-accuracy, per tier.
  * ambiguity   (eval/ambiguity/cases.yml) — clarification precision/recall.
  * adversarial (imported from api/tests/test_ast_guard.ADVERSARIAL_SUITE,
                 not duplicated into YAML — single source of truth; see
                 the --adversarial-only section below) — hard CI gate,
                 0 required, non-zero exit on any leak.

Grading is execution accuracy, not string comparison: both the gold SQL
and the model's generated SQL are run for real against the live database,
and their result sets are compared — sorted, order-insensitive unless the
gold query itself has an ORDER BY (detected by parsing, via pglast, never
by searching the SQL text for the words "order by").

Usage (from eval/, with the api package importable):
    PYTHONPATH=../api ../.venv/bin/python run_eval.py
    PYTHONPATH=../api ../.venv/bin/python run_eval.py --tiers easy medium
    PYTHONPATH=../api ../.venv/bin/python run_eval.py --adversarial-only
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from app.guards import ast_guard  # noqa: E402
from app.pipeline.answer import answer  # noqa: E402
from app.pipeline.errors import Action  # noqa: E402
from app.pipeline.execute import ExecutionError, execute  # noqa: E402
from app.pipeline.generate import build_context  # noqa: E402
from app.pipeline.grading import results_match  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/querywarden"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_golden(tiers: list[str]) -> list[dict]:
    cases = []
    for tier in tiers:
        with open(EVAL_DIR / "golden" / f"{tier}.yml", encoding="utf-8") as f:
            cases.extend(yaml.safe_load(f))
    return cases


def load_ambiguity() -> list[dict]:
    with open(EVAL_DIR / "ambiguity" / "cases.yml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_adversarial() -> list[str]:
    # Single source of truth: the same 61-case suite Phase 01's guard
    # tests already run on every commit. Duplicating it into a YAML file
    # here would just be a second copy that silently drifts out of sync —
    # importing it is the actual fix, not a shortcut.
    import tests.test_ast_guard as guard_tests  # type: ignore[import-not-found]

    return guard_tests.ADVERSARIAL_SUITE


# ---------------------------------------------------------------------------
# Golden suite
# ---------------------------------------------------------------------------

@dataclass
class GoldenCaseResult:
    case_id: str
    difficulty: str
    valid_sql: bool
    correct: bool | None  # None when valid_sql is False — nothing to compare
    verdict: str
    latency_ms: float
    prompt_tokens: int
    eval_tokens: int
    detail: str = ""


def run_golden_suite(ctx, cases: list[dict], tenant_id: int, model: str | None) -> list[GoldenCaseResult]:
    results = []
    for case in cases:
        t0 = time.monotonic()
        outcome = answer(ctx, case["question"], tenant_id=tenant_id, model=model)
        latency_ms = (time.monotonic() - t0) * 1000

        prompt_tokens = eval_tokens = 0
        # answer() doesn't currently surface per-attempt token counts on
        # AnswerOutcome (Phase 04 scope was error handling, not metrics) —
        # tracked as a known gap rather than guessed at; see README.

        if outcome.verdict is not Action.ANSWERED:
            results.append(GoldenCaseResult(
                case["id"], case["difficulty"], valid_sql=False, correct=None,
                verdict=outcome.verdict.value, latency_ms=latency_ms,
                prompt_tokens=prompt_tokens, eval_tokens=eval_tokens,
                detail=outcome.message,
            ))
            continue

        try:
            gold_result = execute(case["sql"], tenant_id=tenant_id)
        except ExecutionError as exc:
            results.append(GoldenCaseResult(
                case["id"], case["difficulty"], valid_sql=True, correct=None,
                verdict="gold_sql_failed", latency_ms=latency_ms,
                prompt_tokens=prompt_tokens, eval_tokens=eval_tokens, detail=str(exc),
            ))
            continue

        correct = results_match(gold_result.rows, outcome.execution.rows, case["sql"])
        results.append(GoldenCaseResult(
            case["id"], case["difficulty"], valid_sql=True, correct=correct,
            verdict=outcome.verdict.value, latency_ms=latency_ms,
            prompt_tokens=prompt_tokens, eval_tokens=eval_tokens,
            detail="" if correct else f"got {outcome.execution.rows[:3]!r} vs gold {gold_result.rows[:3]!r}",
        ))
    return results


# ---------------------------------------------------------------------------
# Ambiguity suite
# ---------------------------------------------------------------------------

@dataclass
class AmbiguityCaseResult:
    case_id: str
    should_clarify: bool
    did_clarify: bool
    correct: bool


def run_ambiguity_suite(ctx, cases: list[dict], tenant_id: int, model: str | None) -> list[AmbiguityCaseResult]:
    results = []
    for case in cases:
        outcome = answer(ctx, case["question"], tenant_id=tenant_id, model=model)
        did_clarify = outcome.verdict is Action.ASK
        results.append(AmbiguityCaseResult(
            case["id"], case["should_clarify"], did_clarify,
            correct=(did_clarify == case["should_clarify"]),
        ))
    return results


# ---------------------------------------------------------------------------
# Adversarial suite
# ---------------------------------------------------------------------------

@dataclass
class AdversarialResult:
    leaked: list[str] = field(default_factory=list)
    total: int = 0

    @property
    def pass_rate(self) -> float:
        return 1.0 if self.total == 0 else 1 - len(self.leaked) / self.total


def run_adversarial_suite(catalog) -> AdversarialResult:
    cases = load_adversarial()
    leaked = [sql for sql in cases if ast_guard.check(sql, catalog=catalog).ok]
    return AdversarialResult(leaked=leaked, total=len(cases))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(golden: list[GoldenCaseResult], ambiguity: list[AmbiguityCaseResult], adversarial: AdversarialResult) -> bool:
    print("=" * 78)
    print("QUERY WARDEN — EVALUATION REPORT")
    print("=" * 78)

    print("\n-- Golden set (execution accuracy) --")
    if not golden:
        print("  (skipped)")
    else:
        tiers = sorted({r.difficulty for r in golden})
        for tier in tiers:
            tier_results = [r for r in golden if r.difficulty == tier]
            n = len(tier_results)
            correct = sum(1 for r in tier_results if r.correct is True)
            valid = sum(1 for r in tier_results if r.valid_sql)
            print(f"  {tier:8} accuracy: {correct}/{n} ({100*correct/n:.0f}%)   valid-SQL: {valid}/{n} ({100*valid/n:.0f}%)")
        n_all = len(golden)
        correct_all = sum(1 for r in golden if r.correct is True)
        valid_all = sum(1 for r in golden if r.valid_sql)
        print(f"  {'overall':8} accuracy: {correct_all}/{n_all} ({100*correct_all/n_all:.0f}%)   valid-SQL: {valid_all}/{n_all} ({100*valid_all/n_all:.0f}%)")

        avg_latency = sum(r.latency_ms for r in golden) / n_all
        p95_latency = sorted(r.latency_ms for r in golden)[int(0.95 * n_all) - 1]
        print(f"\n  avg latency: {avg_latency:.0f}ms   p95 latency: {p95_latency:.0f}ms")
        print("  cost: $0.00 (local Ollama model — no per-token billing)")

        failures = [r for r in golden if r.correct is False or (not r.valid_sql and r.verdict not in ("ask",))]
        if failures:
            print(f"\n  {len(failures)} incorrect/failed case(s):")
            for r in failures[:10]:
                print(f"    [{r.difficulty}] {r.case_id}: verdict={r.verdict}  {r.detail[:100]}")

    print("\n-- Ambiguity set (clarification precision/recall) --")
    if not ambiguity:
        print("  (skipped)")
    else:
        n_amb = len(ambiguity)
        correct_amb = sum(1 for r in ambiguity if r.correct)
        should_clarify = [r for r in ambiguity if r.should_clarify]
        should_not = [r for r in ambiguity if not r.should_clarify]
        recall = sum(1 for r in should_clarify if r.did_clarify) / len(should_clarify) if should_clarify else float("nan")
        false_positive_rate = sum(1 for r in should_not if r.did_clarify) / len(should_not) if should_not else float("nan")
        print(f"  overall accuracy: {correct_amb}/{n_amb} ({100*correct_amb/n_amb:.0f}%)")
        print(f"  recall (asked when it should have):        {100*recall:.0f}%")
        print(f"  false-positive rate (asked when it shouldn't): {100*false_positive_rate:.0f}%")

    print("\n-- Adversarial set (hard CI gate) --")
    print(f"  blocked: {adversarial.total - len(adversarial.leaked)}/{adversarial.total} "
          f"({100*adversarial.pass_rate:.0f}%)   pass-through: {len(adversarial.leaked)} (required: 0)")
    if adversarial.leaked:
        print("  LEAKED:")
        for sql in adversarial.leaked:
            print(f"    {sql!r}")

    print("\n" + "=" * 78)
    gate_passed = len(adversarial.leaked) == 0
    print("RESULT:", "PASS" if gate_passed else "FAIL — adversarial leak(s) present, build blocked")
    print("=" * 78)
    return gate_passed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Query Warden evaluation harness")
    parser.add_argument("--tiers", nargs="+", default=["easy", "medium", "hard"])
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument("--model", default=None)
    parser.add_argument("--adversarial-only", action="store_true", help="Run only the CI gate, skip golden/ambiguity (fast).")
    parser.add_argument("--skip-golden", action="store_true")
    parser.add_argument("--skip-ambiguity", action="store_true")
    args = parser.parse_args()

    with psycopg.connect(DATABASE_URL) as conn:
        ctx = build_context(conn)

    adversarial = run_adversarial_suite(ctx.catalog)

    if args.adversarial_only:
        print(f"Adversarial: {adversarial.total - len(adversarial.leaked)}/{adversarial.total} blocked, "
              f"{len(adversarial.leaked)} leaked")
        sys.exit(0 if not adversarial.leaked else 1)

    golden_results = [] if args.skip_golden else run_golden_suite(ctx, load_golden(args.tiers), args.tenant_id, args.model)
    ambiguity_results = [] if args.skip_ambiguity else run_ambiguity_suite(ctx, load_ambiguity(), args.tenant_id, args.model)

    passed = print_report(golden_results, ambiguity_results, adversarial)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
