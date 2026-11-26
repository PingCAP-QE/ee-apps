import time
from types import SimpleNamespace
from unittest.mock import Mock

import pymysql
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from ci_dashboard.common import db
from ci_dashboard.common.config import load_settings


def mysql_engine(monkeypatch, outcomes, **engine_opts):
    settings = load_settings({"CI_DASHBOARD_DB_URL": "mysql+pymysql://ci:secret@db/dashboard"})
    engine = db.build_engine(settings, **engine_opts)
    connect = Mock(side_effect=outcomes)
    monkeypatch.setattr(engine.dialect.loaded_dbapi, "connect", connect)
    # These tests script whole handshake outcomes; the real TCP path has its
    # own tests with multiple DNS answers and the unmocked PyMySQL driver.
    monkeypatch.setattr(db, "_connect_api_tcp", lambda d, a, p, *_: d.connect(*a, **p))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    return engine, connect


def open_dbapi(engine):
    # Invoke SQLAlchemy's actual connection creator, without its MySQL version probes.
    return engine.pool._creator(None)


def test_connection_eof_retried_before_any_business_sql(monkeypatch, caplog):
    connection = SimpleNamespace(_read_timeout=5, _write_timeout=5)
    error = pymysql.OperationalError(2013, "Lost connection to MySQL server during query")
    engine, connect = mysql_engine(monkeypatch, [error, connection])
    params = {"connect_timeout": 5, "read_timeout": 35, "write_timeout": 30}
    assert open_dbapi(engine) is connection
    assert connect.call_count == 2
    assert connection._read_timeout == 35
    assert connection._write_timeout == 30
    for call in connect.call_args_list:
        assert 0 < call.kwargs["connect_timeout"] <= params["connect_timeout"]
        assert 0 < call.kwargs["read_timeout"] <= 5
        assert call.kwargs["init_command"] == "SET SESSION MAX_EXECUTION_TIME=30000"
    assert "secret" not in caplog.text
    assert engine.pool._pre_ping is True
    assert engine.pool._recycle == 900
    engine.dispose()


@pytest.mark.parametrize("code", [2003, 2006, 2013])
def test_connection_retry_is_bounded_and_preserves_final_error(monkeypatch, code):
    errors = [pymysql.OperationalError(code, "transport failed") for _ in range(2)]
    engine, connect = mysql_engine(monkeypatch, errors)
    with pytest.raises(pymysql.OperationalError) as exc:
        open_dbapi(engine)
    assert exc.value is errors[-1]
    assert connect.call_count == 2
    engine.dispose()


@pytest.mark.parametrize("error", [
    pymysql.OperationalError(1045, "access denied"),
    pymysql.ProgrammingError(1064, "syntax error"),
    pymysql.OperationalError(2013, "Lost connection (The read operation timed out)"),
])
def test_nontransient_or_timeout_connection_errors_not_retried(monkeypatch, error):
    engine, connect = mysql_engine(monkeypatch, [error])
    with pytest.raises(type(error)) as exc:
        open_dbapi(engine)
    assert exc.value is error
    assert connect.call_count == 1
    engine.dispose()


@pytest.mark.parametrize("code", [2003, 2013])
def test_api_retries_only_a_fresh_transport_timeout_with_budget_left(monkeypatch, code):
    timeout = pymysql.OperationalError(code, "connect or greeting timed out")
    connection = SimpleNamespace(_read_timeout=5, _write_timeout=5)
    engine, connect = mysql_engine(monkeypatch, [timeout, connection], retry_connect_timeout=True)
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 0, 2.5, 2.6, 2.6]))
    try:
        assert open_dbapi(engine) is connection
        first, second = connect.call_args_list
        assert first.kwargs["read_timeout"] == 2.5
        assert second.kwargs["read_timeout"] == pytest.approx(2.4)
        assert connection._read_timeout == 35
    finally:
        engine.dispose()


def test_api_keeps_first_handshake_budget_if_halving_leaves_no_room_for_jitter(monkeypatch):
    connection = SimpleNamespace(_read_timeout=5, _write_timeout=5)
    engine, connect = mysql_engine(monkeypatch, [connection], retry_connect_timeout=True)
    # The configured timeout is an integer, but the remaining time can be fractional.
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 2.8]))
    try:
        assert open_dbapi(engine) is connection
        assert connect.call_args.kwargs["read_timeout"] == pytest.approx(2.2)
    finally:
        engine.dispose()


def test_api_does_not_start_second_handshake_after_deadline(monkeypatch):
    timeout = pymysql.OperationalError(2013, "greeting timed out")
    engine, connect = mysql_engine(monkeypatch, [timeout], retry_connect_timeout=True)
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 0, 5]))
    try:
        with pytest.raises(pymysql.OperationalError) as exc:
            open_dbapi(engine)
        assert exc.value is timeout
        assert connect.call_count == 1
    finally:
        engine.dispose()


def test_connection_retry_not_started_when_budget_spent(monkeypatch):
    error = pymysql.OperationalError(2013, "EOF")
    engine, connect = mysql_engine(monkeypatch, [error])
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 0, 5]))
    with pytest.raises(pymysql.OperationalError):
        open_dbapi(engine)
    assert connect.call_count == 1
    engine.dispose()


@pytest.mark.parametrize("before_sleep,after_sleep,sleeps", [
    (4.0, 4.1, 0), (4.8, 4.9, 0), (0.0, 4.9, 1),
])
def test_connection_retry_preserves_original_when_remaining_budget_too_small(
    monkeypatch, before_sleep, after_sleep, sleeps,
):
    original = pymysql.OperationalError(2013, "EOF")
    secondary = pymysql.OperationalError(2003, "connect failed")
    engine, connect = mysql_engine(monkeypatch, [original, secondary])
    sleep = Mock()
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 0, before_sleep, after_sleep]))
    monkeypatch.setattr(db.random, "uniform", lambda *_: 0.1)
    try:
        with pytest.raises(pymysql.OperationalError) as exc:
            open_dbapi(engine)
        assert exc.value is original
        assert connect.call_count == 1
        assert sleep.call_count == sleeps
    finally:
        engine.dispose()


def test_existing_transaction_statement_never_replayed(monkeypatch):
    engine = db.build_engine(load_settings({"CI_DASHBOARD_DB_URL": "sqlite:///:memory:"}))
    execute = Mock(side_effect=OperationalError("INSERT", {}, Exception("2013")))
    with engine.begin() as connection:
        monkeypatch.setattr(connection, "execute", execute)
        with pytest.raises(OperationalError):
            connection.execute(text("INSERT INTO example VALUES (1)"))
    assert execute.call_count == 1
    assert not list(engine.dialect.dispatch.do_connect)
    engine.dispose()


def test_changed_pymysql_timeout_interface_fails_visibly(monkeypatch):
    connection = SimpleNamespace(close=Mock())
    engine, connect = mysql_engine(monkeypatch, [connection])
    with pytest.raises(RuntimeError, match="timeout interface"):
        open_dbapi(engine)
    connection.close.assert_called_once()
    assert connect.call_count == 1
    engine.dispose()


def test_installer_requires_build_engine_timeout_arguments():
    from sqlalchemy import create_engine

    engine = create_engine("mysql+pymysql://ci:secret@db/dashboard")
    db._install_connection_retry(engine)
    try:
        with pytest.raises(ValueError, match="explicit connect/read/write timeouts"):
            open_dbapi(engine)
    finally:
        engine.dispose()
