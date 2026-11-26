from unittest.mock import MagicMock

import pymysql
import pytest

from ci_dashboard.common.config import load_settings
from ci_dashboard.common.db import build_engine


@pytest.mark.parametrize("env,probe,query", [
    ({}, (5, 5), (35, 30)),
    ({"CI_DASHBOARD_DB_CONNECT_TIMEOUT_SECONDS": "60"}, (35, 30), (35, 30)),
])
def test_real_pool_checks_and_replaces_silent_connection_before_business_sql(monkeypatch, env, probe, query):
    settings = load_settings({"CI_DASHBOARD_DB_URL": "mysql+pymysql://ci@db/dashboard", **env})
    engine = build_engine(settings)
    old, new = MagicMock(), MagicMock()
    seen = []

    def limits(conn):
        return conn._read_timeout, conn._write_timeout

    for conn in (old, new):
        conn.character_set_name.return_value = "utf8mb4"
        conn.cursor.return_value.execute.side_effect = (
            lambda sql, *_, c=conn: seen.append((sql, limits(c)))
        )
    monkeypatch.setattr(engine.dialect.loaded_dbapi, "connect", MagicMock(side_effect=[old, new]))
    # Only the server metadata lookup needs a real MySQL server. Keep SQLAlchemy's
    # on_connect (SET NAMES), QueuePool, PyMySQL dialect pre-ping and invalidation path.
    monkeypatch.setattr(engine.dialect, "initialize", lambda c: None)

    def ping(*args, **kwargs):
        seen.append(("ping", limits(old)))
        raise pymysql.OperationalError(2013, "Lost connection (read timed out)")

    old.ping.side_effect = ping
    try:
        with engine.connect() as c:
            assert c.connection.driver_connection is old
            assert limits(old) == query
        with engine.connect() as c:
            assert c.connection.driver_connection is new
            assert limits(new) == query
            c.connection.driver_connection.cursor().execute("SELECT after_checkout")
        assert seen == [
            ("SET NAMES utf8mb4", probe),
            ("ping", probe),
            ("SET NAMES utf8mb4", probe),
            ("SELECT after_checkout", query),
        ]
        assert old.ping.call_count == 1
        assert old.close.call_count == 1
    finally:
        engine.dispose()
