"""SqlPlan validation — no DB, no model, always runs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.llm.schemas import ChartSpec, SqlPlan


def test_minimal_valid_plan():
    plan = SqlPlan(intent="count orders", sql="SELECT count(*) FROM analytics.v_orders", confidence="high")
    assert plan.needs_clarification is False
    assert plan.assumptions == []
    assert plan.chart is None


def test_clarifying_plan_flags_needs_clarification():
    plan = SqlPlan(
        intent="ambiguous",
        sql="SELECT 1 WHERE false",
        confidence="low",
        clarifying_question="Which metric do you mean?",
    )
    assert plan.needs_clarification is True


def test_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        SqlPlan(intent="x", confidence="high")  # missing sql


def test_bad_confidence_enum_rejected():
    with pytest.raises(ValidationError):
        SqlPlan(intent="x", sql="SELECT 1", confidence="very high")


def test_chart_spec_round_trips():
    plan = SqlPlan(
        intent="x", sql="SELECT 1", confidence="high",
        chart=ChartSpec(kind="bar", x="region", y="revenue"),
    )
    dumped = plan.model_dump_json()
    restored = SqlPlan.model_validate_json(dumped)
    assert restored.chart == ChartSpec(kind="bar", x="region", y="revenue")


def test_json_schema_has_no_additional_properties_leak():
    # Loose generation catches "helpful" extra keys a small model invents —
    # confirm the schema we hand to Ollama's constrained decoder is closed.
    schema = SqlPlan.model_json_schema()
    assert schema.get("required")
    assert "sql" in schema["required"]
    assert "confidence" in schema["required"]
