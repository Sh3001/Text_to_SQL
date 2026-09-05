-- Query Warden — an account can be identified by email OR phone number.
--
-- Both columns are nullable with a CHECK that at least one is present, so
-- somebody can sign up with a phone number and never supply an email (or
-- the other way round). Numbers are stored normalised to E.164 by
-- app/auth/identifiers.py; that normalisation is what makes
-- "+91 98765 43210" and "09876543210" resolve to the same row.

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------

ALTER TABLE app.users ADD COLUMN IF NOT EXISTS phone text;

-- Existing rows all have an email, so relaxing these is safe.
ALTER TABLE app.users ALTER COLUMN email DROP NOT NULL;
ALTER TABLE app.users ALTER COLUMN password_hash DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'users_has_an_identifier'
    ) THEN
        ALTER TABLE app.users ADD CONSTRAINT users_has_an_identifier
            CHECK (email IS NOT NULL OR phone IS NOT NULL);
    END IF;
END
$$;

-- Phone numbers are stored normalised to E.164 (app/auth/identifiers.py), so a
-- plain unique index is enough — no lower() needed as for email.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON app.users (phone) WHERE phone IS NOT NULL;

COMMENT ON COLUMN app.users.phone IS 'E.164, normalised on the way in. Null for email-only accounts.';

-- A previous revision of this file created app.otp_codes for passcode
-- sign-in. That feature was removed; drop the table if an older database
-- still has it.
DROP TABLE IF EXISTS app.otp_codes;

-- ---------------------------------------------------------------------------
-- Checkpoint:
--   psql "$CHATBOT_DATABASE_URL" -c "SELECT * FROM app.users"
--       -> ERROR: permission denied for schema app
-- ---------------------------------------------------------------------------
