"""Which tenant the app runs as.

There is no authentication, so there is nobody to ask. The tenant comes
from the server's own configuration and is read here, in one place, so
that it stays a deployment setting rather than drifting back into being
a request field.

That distinction is the whole point of this module. `tenant_id` used to
arrive in the request body, which meant the tenant filtering in
db/00_schema.sql's views and db/02_roles.sql's policies was faithfully
enforcing a number the caller had picked — posting {"tenant_id": 2} read
somebody else's data. Removing authentication doesn't have to reintroduce
that: unauthenticated and caller-controlled are different things, and
this keeps the second one closed.

To serve more than one tenant, you need to know who is asking, which
means authentication again.
"""

from __future__ import annotations

import os

DEFAULT_TENANT_ID = 1


def current_tenant_id() -> int:
    """The tenant every query and conversation is scoped to."""
    raw = os.environ.get("DEFAULT_TENANT_ID")
    if not raw:
        return DEFAULT_TENANT_ID
    try:
        return int(raw)
    except ValueError:
        # A typo here would silently scope every query to the wrong
        # tenant, or to none at all — the views fail closed on a bad
        # value, so this would look like "the database is empty".
        raise ValueError(
            f"DEFAULT_TENANT_ID must be an integer, got {raw!r}"
        ) from None
