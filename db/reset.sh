#!/usr/bin/env bash
# Rebuild querywarden against a local Postgres (e.g. Postgres.app).
# `docker compose up` is the documented path — see repo root README.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PGHOST="${PGHOST:-localhost}"
PGUSER="${PGUSER:-postgres}"
DB="${1:-querywarden}"

echo "==> dropping + recreating $DB"
psql -U "$PGUSER" -h "$PGHOST" -c "DROP DATABASE IF EXISTS $DB;"
psql -U "$PGUSER" -h "$PGHOST" -c "CREATE DATABASE $DB;"

for f in db/00_schema.sql db/01_seed.sql db/02_roles.sql db/03_observability.sql db/04_app.sql db/05_phone_identity.sql; do
    echo "==> applying $f"
    psql -U "$PGUSER" -h "$PGHOST" -d "$DB" -v ON_ERROR_STOP=1 -f "$f"
done

echo "==> done. Verify the floor holds:"
echo "    PGPASSWORD=chatbot_ro psql -U chatbot_ro -h $PGHOST -d $DB -c \"DELETE FROM analytics.orders\""
echo "    (expect: permission denied / read-only transaction)"
