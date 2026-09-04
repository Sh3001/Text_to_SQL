"""Thin wrapper around the local Ollama model, isolated so swapping the
backend later means changing this file only. Structured output is
JSON-schema-constrained decoding (Ollama's `format` parameter) — the
model can't emit a token sequence that violates SqlPlan's shape.

Exceptions are a most-specific-first chain: ModelUnavailableError means
the environment is broken (retrying won't help); OutputParseError means
malformed model output, which the repair loop CAN usefully retry by
feeding the parse error back.
"""

from __future__ import annotations

from dataclasses import dataclass

import ollama
from pydantic import ValidationError

from .schemas import SqlPlan

DEFAULT_MODEL = "qwen2.5-coder:3b"


class GenerationError(RuntimeError):
    """Base for every failure this module raises. Catch this if you don't
    need to distinguish; catch the subclasses below if you do.
    """


class ModelUnavailableError(GenerationError):
    """The Ollama server isn't reachable, or the named model isn't
    pulled. Not something a retry of the same request fixes — it needs an
    operator (start the server, `ollama pull <model>`).
    """


class ModelResponseError(GenerationError):
    """Ollama reached the model but something went wrong server-side
    (a 5xx, a malformed request the server rejected). May be transient.
    """


class OutputParseError(GenerationError):
    """The model produced text that doesn't parse/validate as SqlPlan.
    Distinct from the two above: this is a retryable-via-regeneration
    case (feed the parse error back as repair context), not an infra
    problem — see pipeline/answer.py's repair loop.
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
    matters more than variety, and the eval harness needs reproducible-
    enough behavior to be meaningful run to run.
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
    except ConnectionError as exc:
        raise ModelUnavailableError(
            f"could not reach the Ollama server: {exc}. Is `ollama serve` running?"
        ) from exc
    except ollama.ResponseError as exc:
        if exc.status_code == 404:
            raise ModelUnavailableError(
                f"model '{model}' isn't available (404). Try `ollama pull {model}`."
            ) from exc
        raise ModelResponseError(f"Ollama returned an error ({exc.status_code}): {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — last-resort catch-all, re-raised as our own typed error
        raise ModelResponseError(f"unexpected failure calling Ollama: {exc}") from exc

    content = response.message.content
    try:
        plan = SqlPlan.model_validate_json(content)
    except ValidationError as exc:
        raise OutputParseError(f"model output didn't validate as SqlPlan: {exc}\nraw: {content!r}") from exc

    return GenerationResult(
        plan=plan,
        model=model,
        prompt_eval_count=response.prompt_eval_count or 0,
        eval_count=response.eval_count or 0,
        total_duration_ms=(response.total_duration or 0) / 1e6,
    )
