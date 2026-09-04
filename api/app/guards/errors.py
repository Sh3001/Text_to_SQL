"""Typed rejection reasons for the AST guard — not exceptions, since
success and failure are both ordinary outcomes and the pipeline needs a
structured reason to decide "repair" vs. "block"."""

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

    # Catalog-dependent — only raised when a Catalog is supplied.
    UNKNOWN_TABLE = "unknown_table"
    UNKNOWN_COLUMN = "unknown_column"


# Safety violations, not modelling mistakes — terminal, never fed back to
# the model as a "try again" hint. A missing column is worth a retry; an
# attempted catalog probe is a security event.
TERMINAL_REASONS = frozenset(
    {
        RejectReason.MULTI_STATEMENT,
        RejectReason.DISALLOWED_STATEMENT_TYPE,
        RejectReason.UNSAFE_FUNCTION,
        RejectReason.CATALOG_SCHEMA_ACCESS,
        RejectReason.LOCKING_CLAUSE,
    }
)

# Everything else is repairable: worth one more generation turn with the
# concrete error fed back.
