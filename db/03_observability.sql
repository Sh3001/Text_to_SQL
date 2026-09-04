-- Query Warden — observability: the audit trail and the cost/latency
-- dashboard, both backed by one table. Written by the app's own trusted
-- connection only (chatbot_ro has zero grants on schema audit — see
-- db/02_roles.sql). One row per question, blocked attempts included.

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

-- No REVOKE needed here: chatbot_ro's REVOKE ALL ON SCHEMA audit in
-- db/02_roles.sql already covers every object in this schema, present
-- or future, at the schema level.
