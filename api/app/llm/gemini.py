"""Gemini backend.

Talks to the Generative Language REST API over stdlib urllib rather than
the vendor SDK — the call is one POST, and this keeps the dependency list
short enough to audit.

The interesting part is `to_gemini_schema`. Gemini's `responseSchema` is a
restricted subset of OpenAPI, not full JSON Schema: it rejects `$defs`,
`$ref`, `title`, `additionalProperties` and friends. Pydantic emits all of
those — `SqlPlan.chart` is a nested model, so its schema is a `$ref` into
`$defs`. Sending pydantic's output verbatim is a 400 every time, so the
schema is translated on the way out. That translation is what preserves
the structural guarantee the whole pipeline rests on: the model cannot
return a shape that isn't a SqlPlan.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

#: Gemini 2.5 is closed to new API keys, so this is a current model.
DEFAULT_MODEL = "gemini-3.6-flash"

REQUEST_TIMEOUT_SECONDS = 120

#: JSON Schema keywords Gemini's responseSchema rejects outright.
_UNSUPPORTED_KEYWORDS = frozenset(
    {"title", "default", "$defs", "$schema", "additionalProperties", "examples", "const"}
)


def api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def to_gemini_schema(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rewrites a pydantic JSON Schema into the subset Gemini accepts.

    Three transformations:
      * `$ref` is resolved and inlined, because Gemini has no `$defs`.
      * `anyOf: [X, null]` — which is how pydantic spells `Optional[X]` —
        becomes X with `nullable: true`, Gemini's own idiom.
      * Unsupported keywords are dropped rather than passed through.
    """
    if defs is None:
        defs = schema.get("$defs", {})
    if not isinstance(schema, dict):
        return schema

    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        target = defs.get(name)
        if target is None:
            raise ValueError(f"schema reference {schema['$ref']!r} has no definition")
        return to_gemini_schema(target, defs)

    if "anyOf" in schema:
        variants = [v for v in schema["anyOf"] if v.get("type") != "null"]
        nullable = len(variants) != len(schema["anyOf"])
        if not variants:
            raise ValueError("a schema variant list cannot be only null")
        if len(variants) == 1:
            out = dict(to_gemini_schema(variants[0], defs))
        else:
            out = {"anyOf": [to_gemini_schema(v, defs) for v in variants]}
        if nullable:
            out["nullable"] = True
        for key, value in schema.items():
            if key != "anyOf" and key not in _UNSUPPORTED_KEYWORDS:
                out.setdefault(key, value)
        return out

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_KEYWORDS:
            continue
        if key == "properties":
            out[key] = {k: to_gemini_schema(v, defs) for k, v in value.items()}
        elif key == "items":
            out[key] = to_gemini_schema(value, defs)
        else:
            out[key] = value
    return out


def build_request(system: str, user: str, schema: dict, temperature: float) -> dict:
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": to_gemini_schema(schema),
            "temperature": temperature,
        },
    }


def extract_text(payload: dict) -> str:
    """Pulls the model's text out of a response, turning Gemini's several
    ways of returning nothing into one clear error."""
    feedback = payload.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise GeminiBlocked(f"the prompt was blocked ({feedback['blockReason']})")

    candidates = payload.get("candidates") or []
    if not candidates:
        raise GeminiBlocked("the response contained no candidates")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)

    if not text:
        reason = candidate.get("finishReason", "unknown")
        if reason == "MAX_TOKENS":
            # Repairable: the JSON was cut off mid-object.
            raise GeminiTruncated("the response hit the output token limit before finishing")
        raise GeminiBlocked(f"the response was empty (finishReason={reason})")

    if candidate.get("finishReason") == "MAX_TOKENS":
        raise GeminiTruncated("the response hit the output token limit before finishing")

    return text


class GeminiBlocked(RuntimeError):
    """Gemini returned nothing usable — a safety block or an empty
    candidate. Not something a retry of the same prompt fixes."""


class GeminiTruncated(RuntimeError):
    """The response was cut off, so the JSON is incomplete. Worth another
    attempt, which is why it's distinct from GeminiBlocked."""


class GeminiHTTPError(RuntimeError):
    def __init__(self, status: int, message: str, quota: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        #: On a 429, the quota that was hit, e.g. "20 requests/day per model".
        self.quota = quota


def _quota_summary(error: dict) -> str | None:
    """Pulls the useful half out of a 429 body.

    Worth the parsing: Google's free tier is a *daily* per-model cap, but
    the RetryInfo it returns alongside advertises a delay of a few seconds.
    Measured against the live API, waiting that out does nothing — the
    limit really is daily. So the message needs to say which limit was hit
    and what to do, rather than implying a retry will help.
    """
    for detail in error.get("details", []):
        if not detail.get("@type", "").endswith("QuotaFailure"):
            continue
        for violation in detail.get("violations", []):
            value = violation.get("quotaValue")
            quota_id = violation.get("quotaId", "")
            if value and "PerDay" in quota_id:
                return f"{value} requests/day for this model"
            if value:
                return f"{value} requests for this model"
    return None


def call(system: str, user: str, schema: dict, model: str, temperature: float, key: str) -> tuple[dict, float]:
    """One generateContent call. Returns (payload, elapsed_ms).

    Raises GeminiHTTPError for anything the API rejects; the caller maps
    status codes onto the pipeline's own error taxonomy.
    """
    body = json.dumps(build_request(system, user, schema, temperature)).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/{model}:generateContent?key={key}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        quota = None
        try:
            error = json.loads(raw)["error"]
            detail = error.get("message", raw)[:400]
            quota = _quota_summary(error)
        except (ValueError, KeyError, TypeError):
            detail = raw[:400]
        raise GeminiHTTPError(exc.code, detail, quota) from exc
    except urllib.error.URLError as exc:
        raise GeminiHTTPError(0, f"could not reach the Gemini API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GeminiHTTPError(0, f"the Gemini API did not respond within {REQUEST_TIMEOUT_SECONDS}s") from exc

    return payload, (time.monotonic() - started) * 1000
