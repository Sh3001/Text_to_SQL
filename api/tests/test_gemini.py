"""The Gemini backend.

Most of this is about `to_gemini_schema`. Gemini's `responseSchema` is a
restricted OpenAPI subset, not JSON Schema: it rejects `$defs`, `$ref`,
`title` and `additionalProperties`, all of which pydantic emits. Sending
pydantic's output verbatim is a 400 on every request. That translation is
what keeps the structural guarantee the whole pipeline rests on, so it
gets the most tests.

Nothing here needs an API key or a network call: the translation is pure,
and the status-code mapping is exercised against synthetic errors.
"""

from __future__ import annotations

import json

import pytest

from app.llm import client, gemini
from app.llm.schemas import SqlPlan


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for key in ("LLM_PROVIDER", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL", "OLLAMA_MODEL"):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Schema translation
# ---------------------------------------------------------------------------

def test_the_real_sqlplan_schema_translates_without_unsupported_keywords():
    """The end-to-end assertion: whatever pydantic emits today, nothing
    Gemini rejects survives translation, at any depth."""
    translated = gemini.to_gemini_schema(SqlPlan.model_json_schema())

    banned = {"$defs", "$ref", "title", "additionalProperties", "$schema", "default"}
    found: list[str] = []

    def walk(node, path="root"):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in banned:
                    found.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(translated)
    assert found == [], f"unsupported keywords survived: {found}"


def test_a_nested_model_is_inlined_rather_than_referenced():
    """SqlPlan.chart is a nested model, so pydantic emits a $ref into
    $defs. Gemini has no $defs, so it has to be inlined."""
    raw = SqlPlan.model_json_schema()
    assert "$defs" in raw  # if this ever stops being true, the test below is vacuous

    translated = gemini.to_gemini_schema(raw)
    chart = translated["properties"]["chart"]
    assert "properties" in chart
    assert set(chart["properties"]) == {"kind", "x", "y"}


def test_optional_fields_become_nullable():
    """Pydantic spells Optional[X] as anyOf[X, null]; Gemini spells it
    nullable: true."""
    translated = gemini.to_gemini_schema(SqlPlan.model_json_schema())
    assert translated["properties"]["chart"]["nullable"] is True
    assert translated["properties"]["clarifying_question"]["nullable"] is True
    # A required, non-optional field is untouched.
    assert "nullable" not in translated["properties"]["sql"]


def test_literals_survive_as_enums():
    translated = gemini.to_gemini_schema(SqlPlan.model_json_schema())
    assert set(translated["properties"]["confidence"]["enum"]) == {"high", "medium", "low"}


def test_descriptions_are_kept():
    """They're the field-level instructions to the model — dropping them
    would quietly degrade output quality."""
    translated = gemini.to_gemini_schema(SqlPlan.model_json_schema())
    assert translated["properties"]["sql"]["description"]


def test_required_and_types_are_preserved():
    translated = gemini.to_gemini_schema(SqlPlan.model_json_schema())
    assert translated["type"] == "object"
    assert "sql" in translated["required"]
    assert translated["properties"]["assumptions"]["type"] == "array"
    assert translated["properties"]["assumptions"]["items"]["type"] == "string"


def test_the_translated_schema_is_json_serialisable():
    json.dumps(gemini.to_gemini_schema(SqlPlan.model_json_schema()))


def test_a_dangling_reference_is_an_error_not_a_silent_drop():
    with pytest.raises(ValueError):
        gemini.to_gemini_schema({"$ref": "#/$defs/Missing"}, defs={})


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------

def test_the_system_prompt_goes_in_system_instruction():
    """Not prepended to the user turn: the prompt is ordered stable-part
    first for cache reuse, and Gemini caches systemInstruction separately."""
    body = gemini.build_request("SYSTEM", "USER", SqlPlan.model_json_schema(), 0.1)
    assert body["systemInstruction"]["parts"][0]["text"] == "SYSTEM"
    assert body["contents"][0]["parts"][0]["text"] == "USER"
    assert body["contents"][0]["role"] == "user"


def test_the_request_pins_json_output_and_temperature():
    body = gemini.build_request("s", "u", SqlPlan.model_json_schema(), 0.42)
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["temperature"] == 0.42
    assert config["responseSchema"]["type"] == "object"


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------

def test_text_is_joined_across_parts():
    payload = {"candidates": [{"content": {"parts": [{"text": '{"a":'}, {"text": "1}"}]}}]}
    assert gemini.extract_text(payload) == '{"a":1}'


def test_a_blocked_prompt_is_reported_as_blocked():
    with pytest.raises(gemini.GeminiBlocked):
        gemini.extract_text({"promptFeedback": {"blockReason": "SAFETY"}})


def test_no_candidates_is_reported_as_blocked():
    with pytest.raises(gemini.GeminiBlocked):
        gemini.extract_text({"candidates": []})


def test_a_truncated_response_is_distinct_from_a_blocked_one():
    """It matters: truncation is repairable, a block isn't."""
    payload = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": '{"sql"'}]}}]}
    with pytest.raises(gemini.GeminiTruncated):
        gemini.extract_text(payload)


def test_an_empty_response_names_its_finish_reason():
    payload = {"candidates": [{"finishReason": "RECITATION", "content": {"parts": []}}]}
    with pytest.raises(gemini.GeminiBlocked, match="RECITATION"):
        gemini.extract_text(payload)


# ---------------------------------------------------------------------------
# Error mapping onto the pipeline's taxonomy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected",
    [
        (401, client.ModelUnavailableError),
        (403, client.ModelUnavailableError),
        (404, client.ModelUnavailableError),
        (429, client.ModelUnavailableError),  # quota: retrying burns budget for nothing
        (400, client.ModelResponseError),
        (500, client.ModelResponseError),
        (503, client.ModelResponseError),
        (0, client.ModelUnavailableError),    # network
    ],
)
def test_http_status_maps_to_the_right_failure_class(status, expected):
    mapped = client._map_gemini_status(gemini.GeminiHTTPError(status, "boom"), "some-model")
    assert isinstance(mapped, expected)


def test_a_404_names_the_model_and_how_to_change_it():
    """Gemini retires models, so this error needs to be actionable."""
    mapped = client._map_gemini_status(gemini.GeminiHTTPError(404, "gone"), "gemini-1.0-ancient")
    assert "gemini-1.0-ancient" in str(mapped)
    assert "GEMINI_MODEL" in str(mapped)


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

def test_ollama_is_the_default_provider():
    assert client.provider() == "ollama"
    assert client.default_model_for() == client.OLLAMA_DEFAULT_MODEL


def test_selecting_gemini_changes_the_default_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert client.provider() == "gemini"
    assert client.default_model_for() == gemini.DEFAULT_MODEL


def test_the_model_can_be_pinned_per_provider(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-pinned")
    monkeypatch.setenv("OLLAMA_MODEL", "ollama-pinned")
    assert client.default_model_for("gemini") == "gemini-pinned"
    assert client.default_model_for("ollama") == "ollama-pinned"


def test_gemini_without_a_key_fails_before_any_network_call(monkeypatch):
    """And says which two things to do about it."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(client.ModelUnavailableError) as exc:
        client.generate("system", "user")
    assert "GEMINI_API_KEY" in str(exc.value)
    assert "LLM_PROVIDER=ollama" in str(exc.value)


def test_a_truncated_gemini_response_reaches_the_repair_loop_as_a_parse_error(monkeypatch):
    """Truncation has to surface as OutputParseError, because that's the
    class pipeline/errors.py maps to REPAIR. Anything else gives up."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def fake_call(*args, **kwargs):
        return {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": '{"sql"'}]}}]}, 1.0

    monkeypatch.setattr(gemini, "call", fake_call)
    with pytest.raises(client.OutputParseError):
        client.generate("system", "user")


def test_valid_gemini_json_becomes_a_generation_result(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    plan = {
        "intent": "count orders",
        "assumptions": [],
        "tables_used": ["analytics.v_orders"],
        "sql": "SELECT count(*) FROM analytics.v_orders",
        "chart": None,
        "confidence": "high",
        "clarifying_question": None,
    }

    def fake_call(*args, **kwargs):
        return (
            {
                "candidates": [{"content": {"parts": [{"text": json.dumps(plan)}]}}],
                "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 22},
            },
            123.4,
        )

    monkeypatch.setattr(gemini, "call", fake_call)
    result = client.generate("system", "user")

    assert result.plan.sql == plan["sql"]
    assert result.model == gemini.DEFAULT_MODEL
    assert result.prompt_eval_count == 11
    assert result.eval_count == 22
    assert result.total_duration_ms == 123.4
