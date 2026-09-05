-- Query Warden — application schema: identity and conversation history.
--
-- Two things live here, both written only by the app's own trusted
-- connection. chatbot_ro has zero grants on this schema, same as audit:
-- a generated query can never read a password hash or another user's
-- conversation, because the role it runs as cannot see this schema at all.
--
-- Why identity belongs in the database at all: tenant_id used to arrive
-- in the request body, which meant the tenant filtering in db/02_roles.sql
-- and the view predicates in db/00_schema.sql were enforcing a value the
-- caller chose. app.users is where that value now comes from.

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.users (
    id            bigserial PRIMARY KEY,
    email         text        NOT NULL,
    password_hash text        NOT NULL,   -- scrypt$<salt hex>$<derived key hex>
    display_name  text,
    tenant_id     bigint      NOT NULL REFERENCES analytics.tenants(id),
    role          text        NOT NULL DEFAULT 'member',
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_role_check CHECK (role IN ('member', 'operator'))
);

-- Case-insensitive uniqueness: nobody should be able to register
-- Alice@example.com alongside alice@example.com.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON app.users (lower(email));

COMMENT ON TABLE app.users IS
    'Application identity. tenant_id here is the authoritative source for '
    'analytics.* row filtering — never a request field.';
COMMENT ON COLUMN app.users.role IS
    'member: ask questions. operator: also read the audit log and stats.';

-- ---------------------------------------------------------------------------
-- Conversation history
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.conversations (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    bigint      NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    tenant_id  bigint      NOT NULL REFERENCES analytics.tenants(id),
    title      text        NOT NULL DEFAULT 'New conversation',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.messages (
    id              bigserial   PRIMARY KEY,
    conversation_id uuid        NOT NULL REFERENCES app.conversations(id) ON DELETE CASCADE,
    role            text        NOT NULL,
    content         text        NOT NULL,
    -- The full serialized AnswerOutcome for assistant turns: plan,
    -- assumptions, rows, diagnosis, verdict. Stored whole so reloading a
    -- conversation reproduces exactly what the user saw, without re-running
    -- the query against data that may have changed since.
    outcome         jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT messages_role_check CHECK (role IN ('user', 'assistant'))
);

CREATE INDEX IF NOT EXISTS idx_conversations_user    ON app.conversations (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON app.messages (conversation_id, id);

COMMENT ON TABLE app.conversations IS
    'One row per chat thread. Scoped by user_id AND tenant_id — both are '
    'checked on every read, so a stolen conversation id is not enough.';

-- ---------------------------------------------------------------------------
-- Privileges — the same posture as the audit schema.
-- ---------------------------------------------------------------------------

REVOKE ALL ON SCHEMA app FROM chatbot_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE ALL ON TABLES FROM chatbot_ro;

-- ---------------------------------------------------------------------------
-- Checkpoint — verified against a live database:
--
--   psql "$CHATBOT_DATABASE_URL" -c "SELECT * FROM app.users"
--       -> ERROR: permission denied for schema app
--   psql "$CHATBOT_DATABASE_URL" -c "SELECT * FROM app.conversations"
--       -> ERROR: permission denied for schema app
--
-- Create the first user with:
--   cd api && python -m app.auth.cli create-user you@example.com --role operator
-- ---------------------------------------------------------------------------
