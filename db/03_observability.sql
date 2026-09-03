-- Query Warden — observability: the audit trail and the cost/latency
-- dashboard, both backed by one table.
--
-- audit.query_log is written by the application's own trusted connection
-- (never chatbot_ro — see db/02_roles.sql's REVOKE ALL ON SCHEMA audit,
-- present since Phase 00 specifically so the guarded pipeline can never
-- read or tamper with its own audit trail even if every other layer
-- above it were compromised). One row per question the pipeline
-- answers, blocked attempts included — "records every statement
-- generated, including the blocked ones, which are the interesting
-- ones" is the design doc's own description of what this table is for.

CREATE TABLE IF NOT EXISTS audit.query_log (
    id                  bigserial PRIMARY KEY,
    request_id          uuid NOT NULL,
    occurred_at         timestamptz NOT NULL DEFAULT now(),
    tenant_id           bigint NOT NULL,
    question            text NOT NULL,
    model               text NOT NULL,
    verdict             text NOT NULL,   -- answered | ask | block | diagnose | give_up
    failure_kind        text,            -- ambiguous | unknown_identifier | unsafe | over_budget | ... (see errors.py)
    generated_sql       text,            -- the model's own SQL, before any guard rewrite — the interesting one for a block
    safe_sql            text,            -- guard-approved / re-deparsed SQL that actually ran, if any
    edited              boolean NOT NULL DEFAULT false,  -- true if a human edited the SQL before approval
    repair_attempts     int NOT NULL DEFAULT 0,
    row_count           int,
    duration_ms         double precision,
    message             text NOT NULL,
    -- A blocked/unsafe verdict is the one row type this table exists
    -- for above all others — indexed so "show me every blocked attempt"
    -- stays cheap regardless of table size.
    CONSTRAINT query_log_verdict_check CHECK (
        verdict IN ('answered', 'ask', 'block', 'diagnose', 'give_up')
    )
);

CREATE INDEX IF NOT EXISTS idx_query_log_occurred_at ON audit.query_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_query_log_verdict      ON audit.query_log (verdict) WHERE verdict IN ('block', 'give_up');
CREATE INDEX IF NOT EXISTS idx_query_log_tenant        ON audit.query_log (tenant_id, occurred_at DESC);

COMMENT ON TABLE audit.query_log IS
    'Append-only. One row per question answered, including blocked and given-up attempts. '
    'Written by the app''s own trusted connection only — chatbot_ro has zero grants on the audit schema (db/02_roles.sql).';

-- No UPDATE/DELETE grants are ever issued to any application role on
-- this table — the only supported operation from app code is INSERT.
-- (No REVOKE needed here: chatbot_ro's REVOKE ALL ON SCHEMA audit in
-- db/02_roles.sql already covers every object created in this schema,
-- present or future, because it revokes at the schema level.)
