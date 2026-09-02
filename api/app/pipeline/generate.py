"""Ties schema intelligence (Phase 02) to the LLM client (this phase):
builds the PipelineContext once per process (introspect, render, load the
value index) and turns one question into one GenerationResult.

Schema retrieval is deliberately the simple path the project plan
describes for a schema this size: render everything, every time, rather
than a retrieval step. At 14 tables there's nothing to filter — see the
plan's "under ~40 tables" guidance. ctx is built once and reused across
requests; only introspect/render again when the schema might have
changed (a new deploy), not per question.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ..llm import client, prompts
from ..llm.client import GenerationResult
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


def generate_plan(ctx: PipelineContext, question: str, model: str = client.DEFAULT_MODEL) -> GenerationResult:
    hints = extract_value_hints(question, ctx.value_index)
    bundle = prompts.build_prompt(ctx.schema_ddl, question, hints)
    return client.generate(bundle.system, bundle.user, model=model)
