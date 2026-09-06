"""The one place the pipeline talks to a language model.

Two backends, chosen by LLM_PROVIDER:

  * ollama (default) — a local model, no API key, no bill.
  * gemini           — the hosted Google model, needs GEMINI_API_KEY.

Everything downstream imports the exception types and GenerationResult
from this module and nothing else, which is what makes swapping backends
a change to this file rather than to the pipeline. Both backends produce
SqlPlan through schema-constrained decoding, so the structural guarantee
holds either way — Ollama via its `format` parameter, Gemini via
`responseSchema` (see gemini.py for the schema translation that needs).

Exceptions are a most-specific-first chain: ModelUnavailableError means
the environment is broken (retrying won't help); OutputParseError means
malformed model output, which the repair loop CAN usefully retry by
feeding the parse error back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import ValidationError

from . import gemini
from .schemas import SqlPlan

OLLAMA_DEFAULT_MODEL = "qwen2.5-coder:3b"

#: Kept as a module constant because callers import it by name.
DEFAULT_MODEL = OLLAMA_DEFAULT_MODEL


class GenerationError(RuntimeError):
    """Base for every failure this module raises. Catch this if you don't
    need to distinguish; catch the subclasses below if you do.
    """


class ModelUnavailableError(GenerationError):
    """The backend isn't reachable, the model isn't available, or the
    credentials are wrong. Not something a retry of the same request
    fixes — it needs an operator.
    """


class ModelResponseError(GenerationError):
    """The backend was reached but something went wrong server-side
    (a 5xx, a malformed request it rejected). May be transient.
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


def provider() -> str:
    return os.environ.get("LLM_PROVIDER", "ollama").strip().lower()


def default_model_for(name: str | None = None) -> str:
    """The model actually used when a caller doesn't name one. Provider
    dependent, so `DEFAULT_MODEL` alone isn't enough once Gemini is on."""
    chosen = name or provider()
    if chosen == "gemini":
        return os.environ.get("GEMINI_MODEL", gemini.DEFAULT_MODEL)
    return os.environ.get("OLLAMA_MODEL", OLLAMA_DEFAULT_MODEL)


def generate(
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.1,
    keep_alive: str = "10m",
) -> GenerationResult:
    """One generation call against the configured provider.

    Low temperature by default — this is SQL synthesis against a fixed
    schema, not creative writing; determinism matters more than variety,
    and the eval harness needs reproducible-enough behavior to be
    meaningful run to run.

    `keep_alive` is an Ollama concept (how long to hold the model in
    memory) and is ignored by the Gemini backend.
    """
    if provider() == "gemini":
        return _generate_gemini(system, user, model, temperature)
    return _generate_ollama(system, user, model, temperature, keep_alive)


def _validate(content: str, model: str, prompt_tokens: int, output_tokens: int, elapsed_ms: float):
    try:
        plan = SqlPlan.model_validate_json(content)
    except ValidationError as exc:
        raise OutputParseError(
            f"model output didn't validate as SqlPlan: {exc}\nraw: {content!r}"
        ) from exc
    return GenerationResult(
        plan=plan,
        model=model,
        prompt_eval_count=prompt_tokens,
        eval_count=output_tokens,
        total_duration_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def _generate_gemini(system: str, user: str, model: str | None, temperature: float) -> GenerationResult:
    key = gemini.api_key()
    if not key:
        raise ModelUnavailableError(
            "LLM_PROVIDER=gemini but GEMINI_API_KEY is not set. Put it in api/.env "
            "or switch back with LLM_PROVIDER=ollama."
        )

    name = model or default_model_for("gemini")
    schema = SqlPlan.model_json_schema()

    try:
        payload, elapsed_ms = gemini.call(system, user, schema, name, temperature, key)
    except gemini.GeminiHTTPError as exc:
        raise _map_gemini_status(exc, name) from exc

    try:
        content = gemini.extract_text(payload)
    except gemini.GeminiTruncated as exc:
        # Incomplete JSON. Repairable, so it goes back as a parse failure.
        raise OutputParseError(f"{exc} — the JSON is incomplete") from exc
    except gemini.GeminiBlocked as exc:
        raise ModelResponseError(str(exc)) from exc

    usage = payload.get("usageMetadata") or {}
    return _validate(
        content,
        name,
        usage.get("promptTokenCount", 0),
        usage.get("candidatesTokenCount", 0),
        elapsed_ms,
    )


def _map_gemini_status(exc: gemini.GeminiHTTPError, model: str) -> GenerationError:
    """Maps HTTP status onto the pipeline's taxonomy. The distinction that
    matters: ModelUnavailableError gives up immediately, ModelResponseError
    may be worth another attempt."""
    if exc.status in (401, 403):
        return ModelUnavailableError(f"Gemini rejected the API key: {exc}")
    if exc.status == 404:
        return ModelUnavailableError(
            f"Gemini has no model '{model}': {exc}. Set GEMINI_MODEL to a current one."
        )
    if exc.status == 429:
        # Verified against the live API: the free tier's cap is per day per
        # model, and waiting out the retryDelay Google returns alongside it
        # does not clear it. So this gives up rather than retrying — a
        # repair attempt here would burn the loop on requests that cannot
        # succeed. The quota is per MODEL, so naming that is the actionable
        # part.
        limit = f" (limit: {exc.quota})" if exc.quota else ""
        return ModelUnavailableError(
            f"Gemini quota exhausted for '{model}'{limit}. The free tier counts per model "
            f"per day, so setting GEMINI_MODEL to another one (e.g. gemini-flash-latest) "
            f"gives a fresh allowance, or switch back with LLM_PROVIDER=ollama. Original: {exc}"
        )
    if exc.status == 400:
        return ModelResponseError(f"Gemini rejected the request: {exc}")
    if exc.status == 0:
        return ModelUnavailableError(str(exc))
    return ModelResponseError(f"Gemini returned {exc.status}: {exc}")


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def _generate_ollama(
    system: str, user: str, model: str | None, temperature: float, keep_alive: str
) -> GenerationResult:
    import ollama

    name = model or default_model_for("ollama")
    schema = SqlPlan.model_json_schema()

    try:
        response = ollama.chat(
            model=name,
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
                f"model '{name}' isn't available (404). Try `ollama pull {name}`."
            ) from exc
        raise ModelResponseError(f"Ollama returned an error ({exc.status_code}): {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — last-resort catch-all, re-raised as our own typed error
        raise ModelResponseError(f"unexpected failure calling Ollama: {exc}") from exc

    return _validate(
        response.message.content,
        name,
        response.prompt_eval_count or 0,
        response.eval_count or 0,
        (response.total_duration or 0) / 1e6,
    )
