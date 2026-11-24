"""Deterministic transport faults at real transaction boundaries; no live services."""
import errno
from contextlib import contextmanager
from datetime import date
from unittest.mock import Mock

import pytest
from pymysql.err import OperationalError as MySQLError
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError, OperationalError

from cost_insight.common import db, db_retry
from cost_insight.common.config import GcpBillingSettings, load_settings
from cost_insight.jobs import refresh_attribution_daily as attribution
from cost_insight.jobs import state_store
from cost_insight.jobs import sync_gcp_billing_summary as summary
from test_sync_gcp_billing_summary import _sqlite_engine, _summary_row

DAY = date(2026, 5, 18)
ACCOUNT = "pingcap-testing-account"


def disconnect(code=2013, message="Lost connection"):
    return OperationalError("secret SQL", {"password": "secret parameter"}, MySQLError(code, message))


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(db_retry.time, "sleep", Mock())


class CommitFaultEngine:
    """Inject failures before/after SQLite commits, or fail the next checkout."""

    def __init__(self, engine, *, ack_at=1, fail_connect=(), fail_commit=()):
        self.engine = engine
        self.ack_at = ack_at
        self.fail_connect = fail_connect
        self.fail_commit = fail_commit
        self.calls = 0
        self.error = disconnect()

    @contextmanager
    def begin(self):
        self.calls += 1
        attempt = self.calls
        if attempt in self.fail_connect:
            raise self.error
        with self.engine.begin() as connection:
            yield connection
            if attempt in self.fail_commit:
                raise self.error
        if attempt == self.ack_at:
            raise self.error


def row(resource):
    return summary._normalize_summary_row({**_summary_row(), "resource_name": resource})


def resource_names(engine):
    with engine.connect() as connection:
        return connection.execute(text(
            "SELECT resource_name FROM cost_bq_export_summary_daily ORDER BY resource_name"
        )).scalars().all()


def replace(engine, rows, mode):
    common = {"vendor": "gcp", "account_id": ACCOUNT, "dry_run": False, "batch_size": 1}
    if mode == "partition_usage":
        return summary.replace_summary_partition_usage_dates(
            engine, rows, export_partition_date=DAY,
            usage_start_date=DAY, usage_end_date=DAY, **common,
        )
    if mode == "partitions":
        return summary.replace_summary_partitions(
            engine, rows, row_count=2, export_partition_start=DAY,
            export_partition_end=DAY, **common,
        )
    return summary.replace_summary_usage_dates(
        engine, rows, row_count=2, usage_start_date=DAY, usage_end_date=DAY, **common,
    )


@pytest.mark.parametrize("mode", ["partition_usage", "partitions", "usage"])
@pytest.mark.parametrize("exhaust", [False, True])
def test_replacement_rolls_back_all_batches_and_replays_frozen_input(mode, exhaust, caplog, tmp_path):
    engine = _sqlite_engine(f"sqlite:///{tmp_path}/billing.db")
    summary.write_summary_rows(engine, [row("old")], dry_run=False)
    attempts, inserts, errors, connections = [], 0, [], []
    physical_connections = []

    def fault(connection, cursor, statement, params, context, many):
        nonlocal inserts
        if statement.replace("`", "").lstrip().startswith("DELETE FROM cost_bq_export_summary_daily"):
            attempts.append(resource_names_on(connection))
            connections.append(connection)
            physical_connections.append(connection.connection.driver_connection)
        if statement.replace("`", "").lstrip().startswith("INSERT INTO cost_bq_export_summary_daily"):
            inserts += 1
            if inserts % 2 == 0 and (exhaust or not errors):
                error = disconnect()
                errors.append(error)
                # Close the actual DBAPI connection: server-side work rolls back,
                # and replay must check out a different physical connection.
                connection.invalidate()
                raise error

    event.listen(engine, "before_cursor_execute", fault)
    # A one-shot generator must not be resumed halfway through on retry.
    rows = (row(name) for name in ("new-a", "new-b"))
    try:
        if exhaust:
            with pytest.raises(OperationalError) as raised:
                replace(engine, rows, mode)
            assert raised.value is errors[-1]
            assert resource_names(engine) == ["old"]
        else:
            assert replace(engine, rows, mode) == 2
            assert resource_names(engine) == ["new-a", "new-b"]
        assert attempts == [["old"]] * (3 if exhaust else 2)
        assert len({id(connection) for connection in connections}) == len(attempts)
        assert len({id(connection) for connection in physical_connections}) == len(attempts)
        assert len(db_retry.time.sleep.call_args_list) == (2 if exhaust else 1)
        assert all(0.5 <= call.args[0] <= 2 for call in db_retry.time.sleep.call_args_list)
        assert "secret" not in caplog.text
    finally:
        engine.dispose()


def resource_names_on(connection):
    return connection.execute(text(
        "SELECT resource_name FROM cost_bq_export_summary_daily ORDER BY resource_name"
    )).scalars().all()


@pytest.mark.parametrize("mode", ["partition_usage", "partitions", "usage"])
def test_replacement_commit_accepted_but_ack_lost_is_idempotent(mode):
    engine = _sqlite_engine()
    summary.write_summary_rows(engine, [row("old")], dry_run=False)
    faulty = CommitFaultEngine(engine)
    try:
        assert replace(faulty, (row(name) for name in ("new-a", "new-b")), mode) == 2
        assert faulty.calls == 2
        assert resource_names(engine) == ["new-a", "new-b"]
    finally:
        engine.dispose()


def test_normal_summary_upsert_replays_same_batch_after_lost_ack():
    engine = _sqlite_engine()
    faulty = CommitFaultEngine(engine)
    try:
        assert summary.write_summary_rows(faulty, [row("new")], dry_run=False) == 1
        assert resource_names(engine) == ["new"]
        assert faulty.calls == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize("error", [
    disconnect(1045, "Access denied"), disconnect(1064, "Syntax error"),
    disconnect(1146, "Missing table"), disconnect(2013, "The read operation timed out"),
    disconnect(2006, "write timeout"), ValueError("invalid money"),
    DBAPIError(None, None, RuntimeError("unknown invalidation"), connection_invalidated=True),
])
def test_nontransport_failure_is_not_replayed_at_summary_callsite(error):
    engine = _sqlite_engine()
    summary.write_summary_rows(engine, [row("old")], dry_run=False)
    calls = []

    def fault(connection, cursor, statement, params, context, many):
        if statement.replace("`", "").lstrip().startswith("INSERT INTO cost_bq_export_summary_daily"):
            calls.append(statement)
            raise error

    event.listen(engine, "before_cursor_execute", fault)
    try:
        with pytest.raises(type(error)) as raised:
            replace(engine, iter([row("new")]), "partition_usage")
        assert raised.value is error
        assert len(calls) == 1
        assert resource_names(engine) == ["old"]
        db_retry.time.sleep.assert_not_called()
    finally:
        engine.dispose()


def test_scope_validation_precedes_delete_and_does_not_retry():
    engine = _sqlite_engine()
    summary.write_summary_rows(engine, [row("old")], dry_run=False)
    try:
        with pytest.raises(ValueError, match="outside"):
            replace(engine, iter([{**row("new"), "account_id": "other"}]), "partition_usage")
        assert resource_names(engine) == ["old"]
        db_retry.time.sleep.assert_not_called()
    finally:
        engine.dispose()


def test_summary_sync_bookkeeping_failure_preserves_original(monkeypatch):
    engine = _sqlite_engine()
    original = ValueError("invalid source")
    monkeypatch.setattr(state_store, "mark_job_failed", Mock(side_effect=disconnect()))
    try:
        with pytest.raises(ValueError) as raised:
            summary.run_sync_gcp_billing_summary(
                engine, settings=GcpBillingSettings(), export_partition_start=DAY,
                export_partition_end=DAY, fetch_rows=Mock(side_effect=original),
            )
        assert raised.value is original
        state_store.mark_job_failed.assert_called_once()
    finally:
        engine.dispose()


def test_summary_sync_terminal_ack_loss_does_not_publish_failed_state():
    engine = _sqlite_engine()
    faulty = CommitFaultEngine(engine, ack_at=3)
    try:
        with pytest.raises(OperationalError) as raised:
            summary.run_sync_gcp_billing_summary(
                faulty, settings=GcpBillingSettings(), export_partition_start=DAY,
                export_partition_end=DAY, fetch_rows=lambda **kw: [_summary_row()],
            )
        assert raised.value is faulty.error
        with engine.connect() as c:
            assert c.execute(text("SELECT last_status FROM cost_job_state")).scalar_one() == "succeeded"
    finally:
        engine.dispose()


@pytest.mark.parametrize("origin,invalidated,expected", [
    (OSError(errno.ECONNRESET, "reset"), True, True),
    (OSError(errno.EPIPE, "broken pipe"), False, False),
    (TimeoutError("timed out"), True, False),
    (MySQLError(2006, "gone away"), False, True),
])
def test_transport_classifier_requires_known_origin(origin, invalidated, expected):
    assert db_retry.is_transport_disconnect(DBAPIError(
        None, None, origin, connection_invalidated=invalidated,
    )) is expected


def test_transport_classifier_does_not_retry_wrapped_socket_timeout():
    error = disconnect()
    error.orig.__context__ = TimeoutError()
    assert not db_retry.is_transport_disconnect(error)


def attribution_engine(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("""CREATE TABLE cost_job_state(job_name TEXT PRIMARY KEY,watermark_json TEXT,
            last_started_at TEXT,last_succeeded_at TEXT,last_status TEXT,last_error TEXT,updated_at TEXT)"""))
        c.execute(text("""CREATE TABLE cost_attribution_daily(vendor TEXT,account_id TEXT,
            usage_date TEXT,net_cost REAL)"""))
        c.execute(text("""CREATE TABLE cost_resource_serving_publication(basis_key TEXT,
            vendor TEXT,account_id TEXT,usage_date TEXT)"""))
        c.execute(text("INSERT INTO cost_attribution_daily VALUES ('gcp',:account,:day,5)"),
                  {"account": ACCOUNT, "day": DAY})
        c.execute(text("INSERT INTO cost_resource_serving_publication VALUES ('native','gcp',:account,:day)"),
                  {"account": ACCOUNT, "day": DAY})
    monkeypatch.setattr(attribution, "_summary_insert_statements", lambda **kw: [text(
        "INSERT INTO cost_attribution_daily VALUES (:vendor,:account_id,:start_date,8)"
    )])
    monkeypatch.setattr(attribution, "run_materialize_resource_serving", Mock())
    return engine


def refresh(engine):
    return attribution.run_refresh_cost_attribution_from_summary(
        engine, source=attribution.CostAttributionSource("gcp", ACCOUNT),
        start_date=DAY, end_date=DAY,
    )


def attribution_state(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT last_status FROM cost_job_state")).scalar_one()


@pytest.mark.parametrize("ack_loss", [False, True])
def test_attribution_replay_keeps_rows_invalidation_and_success_atomic(monkeypatch, ack_loss):
    engine = attribution_engine(monkeypatch)
    observed = []
    original = state_store.mark_job_succeeded

    def fault(c, *args):
        observed.append(c.execute(text("SELECT net_cost FROM cost_attribution_daily")).scalars().all())
        assert c.execute(text("SELECT COUNT(*) FROM cost_resource_serving_publication")).scalar_one() == 0
        original(c, *args)
        if not ack_loss and len(observed) == 1:
            raise disconnect()

    monkeypatch.setattr(state_store, "mark_job_succeeded", fault)
    try:
        result = refresh(CommitFaultEngine(engine, ack_at=2) if ack_loss else engine)
        assert result.rows_inserted == 1
        assert observed == [[8], [8]]
        assert attribution_state(engine) == "succeeded"
        attribution.run_materialize_resource_serving.assert_called_once()
    finally:
        engine.dispose()


def test_attribution_exhaustion_rolls_back_and_cleanup_failure_preserves_original(monkeypatch):
    engine = attribution_engine(monkeypatch)
    errors = []

    def fail(*args):
        error = disconnect()
        errors.append(error)
        raise error

    monkeypatch.setattr(state_store, "mark_job_succeeded", fail)
    monkeypatch.setattr(state_store, "mark_job_failed", Mock(side_effect=disconnect()))
    try:
        with pytest.raises(OperationalError) as raised:
            refresh(engine)
        assert len(errors) == 3 and raised.value is errors[-1]
        with engine.connect() as c:
            assert c.execute(text("SELECT net_cost FROM cost_attribution_daily")).scalars().all() == [5]
            assert c.execute(text("SELECT COUNT(*) FROM cost_resource_serving_publication")).scalar_one() == 1
        assert attribution_state(engine) == "running"  # Recovery still manual when bookkeeping fails.
        attribution.run_materialize_resource_serving.assert_not_called()
    finally:
        engine.dispose()


def test_attribution_successful_callback_is_not_an_acknowledged_commit(monkeypatch):
    engine = attribution_engine(monkeypatch)
    # refresh() returns normally, but all three attempts fail before COMMIT.
    faulty = CommitFaultEngine(engine, ack_at=None, fail_commit=(2, 3, 4))
    try:
        with pytest.raises(OperationalError) as raised:
            refresh(faulty)
        assert raised.value is faulty.error
        with engine.connect() as c:
            assert c.execute(text("SELECT net_cost FROM cost_attribution_daily")).scalars().all() == [5]
            assert c.execute(text("SELECT COUNT(*) FROM cost_resource_serving_publication")).scalar_one() == 1
        assert attribution_state(engine) == "failed"
        attribution.run_materialize_resource_serving.assert_not_called()
    finally:
        engine.dispose()


def test_materialization_failure_after_acknowledged_commit_fails_job(monkeypatch):
    engine = attribution_engine(monkeypatch)
    original = ValueError("materialization failed")
    monkeypatch.setattr(attribution, "run_materialize_resource_serving", Mock(side_effect=original))
    try:
        with pytest.raises(ValueError) as raised:
            refresh(engine)
        assert raised.value is original
        with engine.connect() as c:
            assert c.execute(text("SELECT net_cost FROM cost_attribution_daily")).scalars().all() == [8]
        assert attribution_state(engine) == "failed"
    finally:
        engine.dispose()


def test_attribution_unknown_terminal_commit_does_not_overwrite_succeeded(monkeypatch):
    engine = attribution_engine(monkeypatch)
    # Start succeeds, refresh commits but ACK is lost; both replay checkouts fail.
    faulty = CommitFaultEngine(engine, ack_at=2, fail_connect=(3, 4))
    try:
        with pytest.raises(OperationalError) as raised:
            refresh(faulty)
        assert raised.value is faulty.error
        assert attribution_state(engine) == "succeeded"
        attribution.run_materialize_resource_serving.assert_not_called()
    finally:
        engine.dispose()


@pytest.mark.parametrize("name,value", [("CONNECT", "0"), ("CONNECT", "61"),
    ("READ", "0"), ("READ", "3601"), ("WRITE", "-1"), ("WRITE", "3601")])
def test_database_timeouts_are_bounded(name, value):
    with pytest.raises(ValueError, match="TIMEOUT_SECONDS"):
        load_settings({f"COST_INSIGHT_DB_{name}_TIMEOUT_SECONDS": value}, require_database=False)


def test_database_timeout_overrides_preserve_pre_ping_and_sqlite(monkeypatch):
    settings = load_settings({"COST_INSIGHT_DB_URL": "mysql+pymysql://user:pass@host/db",
        "COST_INSIGHT_DB_CONNECT_TIMEOUT_SECONDS": "20", "COST_INSIGHT_DB_READ_TIMEOUT_SECONDS": "900",
        "COST_INSIGHT_DB_WRITE_TIMEOUT_SECONDS": "1200"})
    create = Mock()
    monkeypatch.setattr(db, "create_engine", create)
    db.build_engine(settings)
    assert create.call_args.kwargs["pool_pre_ping"] is True
    assert create.call_args.kwargs["connect_args"] == {
        "connect_timeout": 20, "read_timeout": 900, "write_timeout": 1200,
    }
    db.build_engine(load_settings({"COST_INSIGHT_DB_URL": "sqlite://"}))
    assert create.call_args.kwargs["connect_args"] == {}
