"""SqlPlan — the structured output every generation call returns. The
pipeline never scrapes SQL out of free text; `tables_used` and
`confidence` are what the downstream repair/clarify gates key off (see
pipeline/generate.py and the project plan's error taxonomy).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ChartSpec(BaseModel):
    kind: Literal["bar", "line", "none"]
    x: str
    y: str


class SqlPlan(BaseModel):
    intent: str = Field(description="The user's question, restated in one sentence.")
    assumptions: list[str] = Field(
        default_factory=list,
        description="Every non-obvious choice made: date range, which column, how a term was defined.",
    )
    tables_used: list[str] = Field(
        default_factory=list, description="Exact view names referenced in `sql`, e.g. analytics.v_orders."
    )
    sql: str = Field(description="A single read-only SELECT statement, or a harmless placeholder if clarifying_question is set.")
    chart: Optional[ChartSpec] = Field(default=None, description="Suggested chart, or null if the result isn't chart-worthy.")
    confidence: Literal["high", "medium", "low"]
    clarifying_question: Optional[str] = Field(
        default=None, description="Set instead of guessing when the question is genuinely ambiguous."
    )

    @property
    def needs_clarification(self) -> bool:
        return self.clarifying_question is not None
