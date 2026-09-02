"""Execution envelope against the real chatbot_ro role — the layer-4 half
of "guard.check() approved it, now actually run it."
"""

from __future__ import annotations

import pytest

from app.pipeline.execute import ExecutionError, execute

from .conftest import requires_db


@requires_db
def test_execute_returns_real_rows(pg_conn):
    result = execute("SELECT count(*) AS n FROM analytics.v_orders LIMIT 1", tenant_id=1)
    assert result.columns == ["n"]
    assert result.row_count == 1
    assert result.rows[0][0] > 0


@requires_db
def test_execute_without_the_right_tenant_sees_nothing(pg_conn):
    result = execute("SELECT count(*) AS n FROM analytics.v_orders", tenant_id=999)
    assert result.rows[0][0] == 0


@requires_db
def test_execute_rejects_a_write_even_if_it_somehow_reached_here(pg_conn):
    # Defense in depth, exercised for real: the guard should never approve
    # a DELETE, but if one reached execute() anyway, chatbot_ro's own
    # read-only session setting is the backstop — see db/02_roles.sql.
    with pytest.raises(ExecutionError):
        execute("DELETE FROM analytics.orders", tenant_id=1)


@requires_db
def test_execute_surfaces_a_real_sqlstate_on_error(pg_conn):
    with pytest.raises(ExecutionError) as exc_info:
        execute("SELECT * FROM analytics.v_nonexistent_view", tenant_id=1)
    assert exc_info.value.sqlstate is not None


@requires_db
def test_execute_leaves_no_lasting_transaction_state(pg_conn):
    # Two calls in a row must not interfere via leftover session state
    # (a stuck app.tenant_id from a previous call, an open transaction).
    r1 = execute("SELECT count(*) AS n FROM analytics.v_orders", tenant_id=1)
    r2 = execute("SELECT count(*) AS n FROM analytics.v_orders", tenant_id=999)
    assert r1.rows[0][0] > 0
    assert r2.rows[0][0] == 0
