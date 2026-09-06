"""Ties schema intelligence to the LLM client: builds the PipelineContext
once per process (introspect, render, load the value index) and turns
one question into one GenerationResult.

Schema retrieval is deliberately simple for a schema this size: render
everything, every time, rather than a retrieval step. ctx is built once
and reused across requests, not rebuilt per question.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ..llm import client, prompts
from ..llm.client import GenerationResult
from ..llm.prompts import RepairAttempt
from ..schema import introspect, render, semantic, value_index
from ..schema.catalog import SnapshotCatalog
from .hints import extract_value_hints


@dataclass(frozen=True)
class PipelineContext:
    schema_ddl: str
    snapshot: introspect.Snapshot
    catalog: SnapshotCatalog
    value_index: value_index.ValueIndex


def build_context(conn: psycopg.Connection) -> PipelineContext:
    snapshot = introspect.introspect(conn)
    ddl = render.render(snapshot)
    vindex = value_index.build(conn, semantic.value_synonyms())
    return PipelineContext(
        schema_ddl=ddl,
        snapshot=snapshot,
        catalog=SnapshotCatalog(snapshot),
        value_index=vindex,
    )


def generate_plan(
    ctx: PipelineContext,
    question: str,
    model: str | None = None,
    repair_attempts: list[RepairAttempt] | None = None,
) -> GenerationResult:
    """`model=None` means "whatever the configured provider defaults to".
    Binding client.DEFAULT_MODEL as the default here would freeze the
    Ollama model at import time and quietly ignore LLM_PROVIDER=gemini."""
    hints = extract_value_hints(question, ctx.value_index)
    bundle = prompts.build_prompt(ctx.schema_ddl, question, hints, repair_attempts)
    return client.generate(bundle.system, bundle.user, model=model)
