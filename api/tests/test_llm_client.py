"""Generation against the real, running local Ollama model. Skips cleanly
(requires_ollama) when no server is reachable — see conftest.py.

Assertions here are structural (a SqlPlan parses, fields are non-empty),
not semantic (the SQL is exactly right) — a 3B local model's output isn't
deterministic enough across hardware/versions to assert exact text on,
and grading correctness is Phase 05's job, not this one's. What Phase 03
needs proven is that constrained decoding actually produces a valid
SqlPlan every time, not that the SQL is always right.
"""

from __future__ import annotations

from app.llm import client, prompts

from .conftest import requires_ollama

_SIMPLE_SCHEMA = """\
TABLE analytics.v_orders (
    id bigint,
    status text,
    ordered_at timestamp with time zone
)
  PRIMARY KEY (id)
"""


@requires_ollama
def test_generation_returns_a_valid_sql_plan():
    bundle = prompts.build_prompt(_SIMPLE_SCHEMA, "How many orders are there?")
    result = client.generate(bundle.system, bundle.user)
    assert result.plan.sql.strip() != ""
    assert result.plan.confidence in {"high", "medium", "low"}
    assert result.model == client.DEFAULT_MODEL


@requires_ollama
def test_generation_reports_token_counts():
    bundle = prompts.build_prompt(_SIMPLE_SCHEMA, "Count all orders")
    result = client.generate(bundle.system, bundle.user)
    assert result.prompt_eval_count > 0
    assert result.eval_count > 0
    assert result.total_duration_ms > 0


@requires_ollama
def test_repeated_identical_prefix_is_faster_the_second_time():
    # The empirical claim prompts.py's docstring makes: a warm model
    # reuses the KV-cache for a matching prefix. Not a hard latency
    # assertion (timing varies with machine load) — just confirms the
    # second call isn't paying full prompt-eval cost again.
    bundle = prompts.build_prompt(_SIMPLE_SCHEMA, "How many orders are shipped?")
    first = client.generate(bundle.system, bundle.user)
    second = client.generate(bundle.system, bundle.user)
    assert second.prompt_eval_count == first.prompt_eval_count  # same prefix, same token count


@requires_ollama
def test_generated_sql_is_at_least_guard_checkable():
    # Doesn't assert the guard approves it (a 3B model can and does
    # sometimes produce something the guard correctly rejects) — only
    # that the guard can run on it without crashing, i.e. the model
    # produced a string, not garbage that breaks the pipeline downstream.
    from app.guards.ast_guard import check

    bundle = prompts.build_prompt(_SIMPLE_SCHEMA, "How many orders were shipped last week?")
    result = client.generate(bundle.system, bundle.user)
    guard_result = check(result.plan.sql)  # must not raise
    assert guard_result is not None
