-- Query Warden — password reset.
--
-- A reset token is a bearer credential that changes a password, so it is
-- treated like one: 256 bits of randomness, hashed at rest, single use,
-- and short-lived. Because it's long there is no brute-force surface and
-- no attempt ceiling is needed — unlike a 6-digit code, where the
-- ceiling IS the security.
--
-- Delivery is deliberately not part of this. This project has no mail or
-- SMS provider wired up, so a reset is generated and handed to the person
-- by an operator (see `python -m app.auth.cli reset-password`). If a
-- sender is added later, it delivers this same token and nothing here
-- changes.

-- ---------------------------------------------------------------------------
-- When a password last changed — used to cut existing sessions loose.
-- ---------------------------------------------------------------------------
-- Tokens are stateless JWTs, so without this a stolen session would stay
-- valid for its full lifetime AFTER the victim resets their password,
-- which is the one moment they most expect to be safe. api/app/auth/deps.py
-- rejects any token issued before this timestamp.

ALTER TABLE app.users
    ADD COLUMN IF NOT EXISTS password_changed_at timestamptz NOT NULL DEFAULT now();

COMMENT ON COLUMN app.users.password_changed_at IS
    'Any session token issued before this instant is refused. Set on every '
    'password change or reset.';

-- ---------------------------------------------------------------------------
-- Outstanding resets
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.password_resets (
    id          bigserial   PRIMARY KEY,
    user_id     bigint      NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
    token_hash  text        NOT NULL,   -- scrypt, same format as passwords
    expires_at  timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Verification looks up by hash prefix then confirms; see reset.py for why
-- the lookup can't be by token value.
CREATE INDEX IF NOT EXISTS idx_password_resets_user
    ON app.password_resets (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_password_resets_live
    ON app.password_resets (expires_at) WHERE consumed_at IS NULL;

COMMENT ON TABLE app.password_resets IS
    'Single-use password reset tokens. A used or expired row is kept rather '
    'than deleted so a replay is distinguishable from a token that never '
    'existed; sweep old rows on a schedule.';

-- Inherits the schema-level REVOKE from db/04_app.sql — chatbot_ro cannot
-- read this table, which would otherwise let a generated query lift a live
-- reset token and take over an account.
ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE ALL ON TABLES FROM chatbot_ro;

-- ---------------------------------------------------------------------------
-- Checkpoint:
--   psql "$CHATBOT_DATABASE_URL" -c "SELECT * FROM app.password_resets"
--       -> ERROR: permission denied for schema app
-- ---------------------------------------------------------------------------
