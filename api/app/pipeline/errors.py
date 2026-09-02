"""The error taxonomy — every failure this pipeline can hit, named once,
each mapped to exactly one recovery action. This is the table from the
project plan's "Error handling" section, made real: the distinction that
matters throughout is which failures are worth another model turn
(REPAIR) and which are not (BLOCK, ASK, GIVE_UP are all terminal for this
question — a security violation, an ambiguous question, and an exhausted
repair budget all deserve different endings, but none of them loop back
into generation with the failure as a hint).

Adapted from the plan for the actual backend in use: Claude's `stop_reason
== "refusal"` has no Ollama equivalent (Ollama has no distinct safety-
classifier refusal concept), so that row is folded into ModelUnavailable/
ModelResponse — see llm/client.py's exception hierarchy, which this module
classifies rather than duplicates.
"""

from __future__ import annotations

from enum import Enum

from ..guards.errors import RejectReason
from .execute import ExecutionError

# Postgres SQLSTATEs this pipeline gives a specific recovery to. Everything
# else falls back to a generic, still-repairable EXECUTION_ERROR.
_SQLSTATE_SYNTAX_ERROR = "42601"
_SQLSTATE_UNDEFINED_TABLE = "42P01"
_SQLSTATE_UNDEFINED_COLUMN = "42703"
_SQLSTATE_QUERY_CANCELED = "57014"  # statement_timeout firing


class Action(str, Enum):
    ASK = "ask"  # ask the user one specific question; never guess
    REPAIR = "repair"  # feed the real error back, bounded retry
    BLOCK = "block"  # terminal, audit-logged, never retried
    DIAGNOSE = "diagnose"  # zero rows: explain which filter did it
    GIVE_UP = "give_up"  # repair budget exhausted; say so plainly
    ANSWERED = "answered"  # not a failure at all


class FailureKind(str, Enum):
    AMBIGUOUS = "ambiguous"
    UNKNOWN_IDENTIFIER = "unknown_identifier"
    SYNTAX_ERROR = "syntax_error"
    UNSAFE = "unsafe"
    OVER_BUDGET = "over_budget"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    ZERO_ROWS = "zero_rows"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_OUTPUT_INVALID = "model_output_invalid"


ACTION_FOR_KIND: dict[FailureKind, Action] = {
    FailureKind.AMBIGUOUS: Action.ASK,
    FailureKind.UNKNOWN_IDENTIFIER: Action.REPAIR,
    FailureKind.SYNTAX_ERROR: Action.REPAIR,
    FailureKind.UNSAFE: Action.BLOCK,
    FailureKind.OVER_BUDGET: Action.REPAIR,
    FailureKind.TIMEOUT: Action.ASK,
    FailureKind.EXECUTION_ERROR: Action.REPAIR,
    FailureKind.ZERO_ROWS: Action.DIAGNOSE,
    FailureKind.MODEL_UNAVAILABLE: Action.GIVE_UP,
    FailureKind.MODEL_OUTPUT_INVALID: Action.REPAIR,
}

#: Mirrors guards/errors.py's TERMINAL_REASONS at this taxonomy's level —
#: a guard rejection in this set is BLOCK regardless of what ACTION_FOR_KIND
#: says for UNKNOWN_IDENTIFIER/SYNTAX_ERROR, because it's the *reason*, not
#: the surface FailureKind, that determines terminality here.
_GUARD_REASON_TO_KIND: dict[RejectReason, FailureKind] = {
    RejectReason.SYNTAX_ERROR: FailureKind.SYNTAX_ERROR,
    RejectReason.EMPTY_STATEMENT: FailureKind.SYNTAX_ERROR,
    RejectReason.UNKNOWN_TABLE: FailureKind.UNKNOWN_IDENTIFIER,
    RejectReason.UNKNOWN_COLUMN: FailureKind.UNKNOWN_IDENTIFIER,
    RejectReason.MULTI_STATEMENT: FailureKind.UNSAFE,
    RejectReason.DISALLOWED_STATEMENT_TYPE: FailureKind.UNSAFE,
    RejectReason.UNSAFE_FUNCTION: FailureKind.UNSAFE,
    RejectReason.CATALOG_SCHEMA_ACCESS: FailureKind.UNSAFE,
    RejectReason.LOCKING_CLAUSE: FailureKind.UNSAFE,
}


def classify_guard_rejection(reason: RejectReason, terminal: bool) -> tuple[FailureKind, Action]:
    kind = _GUARD_REASON_TO_KIND.get(reason, FailureKind.SYNTAX_ERROR)
    # `terminal` from the guard itself is authoritative (guards/errors.py
    # TERMINAL_REASONS) — trust it over the static table above in case the
    # two taxonomies ever drift, rather than silently retrying a security
    # violation because this module's mapping fell out of sync.
    action = Action.BLOCK if terminal else ACTION_FOR_KIND[kind]
    return kind, action


def classify_execution_error(exc: ExecutionError) -> tuple[FailureKind, Action]:
    sqlstate = exc.sqlstate or ""
    if sqlstate == _SQLSTATE_SYNTAX_ERROR:
        return FailureKind.SYNTAX_ERROR, Action.REPAIR
    if sqlstate in (_SQLSTATE_UNDEFINED_TABLE, _SQLSTATE_UNDEFINED_COLUMN):
        return FailureKind.UNKNOWN_IDENTIFIER, Action.REPAIR
    if sqlstate == _SQLSTATE_QUERY_CANCELED:
        return FailureKind.TIMEOUT, Action.ASK
    return FailureKind.EXECUTION_ERROR, Action.REPAIR
