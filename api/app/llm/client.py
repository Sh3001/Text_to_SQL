"""Thin wrapper around the local Ollama model. Isolated in its own module
so the rest of the pipeline depends on `generate()`'s signature, not on
Ollama's specific request/response shapes — swapping the backend later
(a hosted API, a different local model) means changing this file only.

Structured output is JSON-schema-constrained decoding (Ollama's `format`
parameter, verified against the actual running server — see the Phase 03
build notes), not a hand-parsed convention: the model literally cannot
emit a token sequence that violates SqlPlan's shape. Free-text parsing of
a code fence was deliberately never on the table.
"""

from __future__ import annotations

from dataclasses import dataclass

import ollama

from .schemas import SqlPlan

DEFAULT_MODEL = "qwen2.5-coder:3b"


class GenerationError(RuntimeError):
    """The model produced text that doesn't parse as a SqlPlan, or the
    Ollama server couldn't be reached. Distinguished from a GuardResult
    rejection: this is a pipeline/infra failure, not a judgment about the
    generated SQL — see pipeline/generate.py for how the two are handled
    differently (this is not retried the same way a guard rejection is).
    """


@dataclass(frozen=True)
class GenerationResult:
    plan: SqlPlan
    model: str
    prompt_eval_count: int
    eval_count: int
    total_duration_ms: float


def generate(
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.1,
    keep_alive: str = "10m",
) -> GenerationResult:
    """One generation call. Low temperature by default — this is SQL
    synthesis against a fixed schema, not creative writing; determinism
    matters more than variety, and the eval harness (Phase 05) needs
    reproducible-enough behavior to be meaningful run to run.
    """
    schema = SqlPlan.model_json_schema()
    try:
        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format=schema,
            options={"temperature": temperature},
            keep_alive=keep_alive,
        )
    except Exception as exc:  # ollama raises its own ResponseError/ConnectionError types
        raise GenerationError(f"Ollama request failed: {exc}") from exc

    content = response.message.content
    try:
        plan = SqlPlan.model_validate_json(content)
    except Exception as exc:
        raise GenerationError(f"model output didn't validate as SqlPlan: {exc}\nraw: {content!r}") from exc

    return GenerationResult(
        plan=plan,
        model=model,
        prompt_eval_count=response.prompt_eval_count or 0,
        eval_count=response.eval_count or 0,
        total_duration_ms=(response.total_duration or 0) / 1e6,
    )
