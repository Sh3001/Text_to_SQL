-- Query Warden — authentication removed.
--
-- Accounts, sessions and password resets are gone. Conversation history
-- stays, but is no longer owned by anyone: this is now a single shared
-- workspace, and every thread is visible to every user of the instance.
--
-- ONE THING THIS DOES NOT GIVE BACK. Before auth existed, `tenant_id`
-- arrived in the request body, which meant the tenant filtering in
-- db/00_schema.sql's views and db/02_roles.sql's policies was enforcing a
-- number the caller chose — posting {"tenant_id": 2} read another
-- tenant's data. Removing auth does not reintroduce that: the tenant now
-- comes from DEFAULT_TENANT_ID in the server's own configuration and is
-- not a request field at all. Unauthenticated is not the same as
-- caller-controlled, and api/tests/test_api.py holds that line.

-- ---------------------------------------------------------------------------
-- Conversations lose their owner
-- ---------------------------------------------------------------------------

ALTER TABLE app.conversations DROP CONSTRAINT IF EXISTS conversations_user_id_fkey;
DROP INDEX IF EXISTS app.idx_conversations_user;
ALTER TABLE app.conversations DROP COLUMN IF EXISTS user_id;

CREATE INDEX IF NOT EXISTS idx_conversations_recent
    ON app.conversations (tenant_id, updated_at DESC);

COMMENT ON TABLE app.conversations IS
    'One row per chat thread, scoped only by tenant. There are no accounts, '
    'so every thread is visible to anyone who can reach the instance.';

-- ---------------------------------------------------------------------------
-- Identity tables go
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS app.password_resets;   -- referenced users, so first
DROP TABLE IF EXISTS app.users;

-- ---------------------------------------------------------------------------
-- Checkpoint — the schema-level REVOKE from db/04_app.sql still stands, so
-- chatbot_ro cannot read conversation history:
--
--   psql "$CHATBOT_DATABASE_URL" -c "SELECT * FROM app.conversations"
--       -> ERROR: permission denied for schema app
-- ---------------------------------------------------------------------------
