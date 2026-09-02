"""Typed rejection reasons for the AST guard.

Deliberately not exceptions used for control flow — the guard runs on every
generated query, success and failure are both ordinary outcomes, and the
pipeline needs a structured reason to decide "repair" vs. "block" (see the
error taxonomy in the project plan). Exceptions are reserved for genuinely
unexpected states (a catalog object that doesn't implement the protocol).
"""

from __future__ import annotations

from enum import Enum


class RejectReason(str, Enum):
    # Structural — caught before any semantic check runs.
    SYNTAX_ERROR = "syntax_error"
    EMPTY_STATEMENT = "empty_statement"
    MULTI_STATEMENT = "multi_statement"
    DISALLOWED_STATEMENT_TYPE = "disallowed_statement_type"

    # Semantic — the statement parses and is a bare SELECT, but something
    # inside it reaches for a capability the pipeline doesn't grant.
    UNSAFE_FUNCTION = "unsafe_function"
    CATALOG_SCHEMA_ACCESS = "catalog_schema_access"
    LOCKING_CLAUSE = "locking_clause"

    # Catalog-dependent — only raised when a Catalog is supplied (Phase 02+).
    UNKNOWN_TABLE = "unknown_table"
    UNKNOWN_COLUMN = "unknown_column"


# Reasons that are safety violations, not modelling mistakes: the pipeline
# audit-logs these and returns a terminal rejection. It never feeds the
# rejection reason back to the model as a "try again" hint — see the
# repair-loop-vs-reject-path distinction in the plan. A missing column is
# worth a second attempt; an attempted catalog probe is a security event.
TERMINAL_REASONS = frozenset(
    {
        RejectReason.MULTI_STATEMENT,
        RejectReason.DISALLOWED_STATEMENT_TYPE,
        RejectReason.UNSAFE_FUNCTION,
        RejectReason.CATALOG_SCHEMA_ACCESS,
        RejectReason.LOCKING_CLAUSE,
    }
)

# Everything else (SYNTAX_ERROR, EMPTY_STATEMENT, UNKNOWN_TABLE,
# UNKNOWN_COLUMN) is repairable: worth one more generation turn with the
# concrete error fed back, up to the pipeline's bounded retry count.
