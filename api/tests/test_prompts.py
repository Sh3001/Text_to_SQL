"""Prompt construction — deterministic, no DB, no model."""

from __future__ import annotations

from app.llm.prompts import build_prompt, build_system_prompt, build_user_message


def test_system_prompt_contains_hard_rules_and_schema():
    system = build_system_prompt("TABLE analytics.v_orders (id bigint)")
    assert "SELECT statement" in system
    assert "TABLE analytics.v_orders" in system
    assert "clarifying_question" in system


def test_system_prompt_is_deterministic():
    a = build_system_prompt("TABLE x (id bigint)")
    b = build_system_prompt("TABLE x (id bigint)")
    assert a == b


def test_user_message_without_hints():
    msg = build_user_message("How many orders?")
    assert msg == "Q: How many orders?"


def test_user_message_with_hints_appends_after_question():
    msg = build_user_message("Orders in California", ["\"California\" -> v_customers.state = 'CA' (resolved via synonym)"])
    assert msg.startswith("Q: Orders in California")
    assert "resolved via synonym" in msg
    assert msg.index("Q:") < msg.index("California\" ->")


def test_build_prompt_bundle():
    bundle = build_prompt("TABLE x (id bigint)", "how many rows", ["hint 1"])
    assert "TABLE x" in bundle.system
    assert "hint 1" in bundle.user
    assert "how many rows" in bundle.user
