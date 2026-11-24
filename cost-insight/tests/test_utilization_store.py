"""Offline lifecycle tests. These are not TiDB/cloud/canary performance evidence."""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError

from cost_insight.jobs.state_store import checkpoint_job_watermark
from cost_insight.utilization import policy, store
from cost_insight.utilization.schema import daily, metadata, observations, weekly

WEEK = date(2026, 11, 16)
NOW = policy.day_start(WEEK) + timedelta(hours=12)


def contract(denominator="physical"):
    return {
        "namespace": "QCE/CVM",
        "metric": "CpuUsage",
        "period": 60,
        "source_stat": "avg",
        "unit": "percent",
        "normalized_unit": "cores",
        "semantic_version": "v1",
        "denominator": denominator,
    }


def contracts():
    return {
        "cvm": {
            "cpu": {
                "contract": contract(),
                "verified_horizon_seconds": 3 * 86400,
                "safety_margin_seconds": 60,
                "timestamp_alignment_verified": True,
                "scope_contracts": {"job": contract("requests"), "repo": contract("requests")},
            }
        }
    }


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'utilization.db'}")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("""
            CREATE TABLE cost_job_state (
              job_name TEXT PRIMARY KEY, watermark_json JSON NOT NULL,
              last_started_at DATETIME, last_succeeded_at DATETIME,
              last_status TEXT NOT NULL, last_error TEXT, updated_at DATETIME
            )
        """)
        )
    yield engine
    engine.dispose()


def ready(engine, now=NOW, expected=None):
    store.configure(
        engine, start_week=WEEK, contracts=expected or contracts(), enabled=True, now=now
    )
    assert not store.maintain(engine, now=now)["state"]["paused"]


def row(
    day=WEEK,
    *,
    scope="account_class",
    key=None,
    used=1,
    capacity=2,
    peak=70,
    complete=False,
    cutoff=None,
):
    cutoff = cutoff or policy.day_start(day) + timedelta(hours=12)
    seconds = 86400 if complete else 43200
    return {
        "scope": scope,
        "resource_class": "cvm",
        "resource_key": hashlib.sha256((key or scope).encode()).hexdigest(),
        "resource_spec": "2c/4GiB",
        "repo": "pingcap/tidb",
        "job_name": "test/unit",
        "cutoff": cutoff,
        "evidence_cutoff": cutoff,
        "status": "complete" if complete else "partial",
        "metrics_json": {
            "cpu": {
                "contract": contract("requests" if scope in {"job", "repo"} else "physical"),
                "used_integral": used * seconds,
                "capacity_integral": capacity * seconds,
                "total_used_integral": used * seconds,
                "expected_capacity_integral": capacity * seconds,
                "expected_seconds": seconds,
                "observed_seconds": seconds,
                "avg": 100 * used / capacity if capacity else None,
                "peak": peak,
                "peak_at": cutoff.isoformat(),
                "coverage": 1,
                "peak_coverage": 1,
                "latest": 50,
                "as_of": cutoff.isoformat(),
                "quality": [],
            }
        },
    }


def observation(when=NOW, *, resource_id="uid-1", cpu=2, **extra):
    return {
        "resource_type": "pod",
        "resource_id": resource_id,
        "observed_from": when,
        "last_seen": when,
        "cluster": "test",
        "namespace": "jenkins-tidb",
        "name": "pod-1",
        "attributes_json": {
            "repo": "pingcap/tidb",
            "job_name": "test/unit",
            "cpu_request": cpu,
            "memory_request": 1024,
            "cpu_limit": None,
            "memory_limit": None,
        },
        **extra,
    }


def rows(engine, table):
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(select(table)).mappings()]


def set_state(engine, **changes):
    with engine.begin() as connection:
        state = store.load_state(connection)
        state.update(changes)
        checkpoint_job_watermark(connection, policy.GUARD_KEY, state)


def test_disabled_default_and_maintenance_readiness(engine):
    state = store.configure(engine, start_week=WEEK, contracts=contracts(), now=NOW)
    assert not state["enabled"]
    for operation in (
        lambda: store.replace_day(engine, WEEK, [row()], now=NOW),
        lambda: store.save_observations(engine, [observation()], now=NOW),
    ):
        with pytest.raises(ValueError, match="blocked"):
            operation()
    store.configure(engine, start_week=WEEK, contracts=contracts(), enabled=True, now=NOW)
    with pytest.raises(ValueError, match="maintenance_uninitialized"):
        store.replace_day(engine, WEEK, [row()], now=NOW)
    state = store.maintain(engine, now=NOW)["state"]
    assert state["counts_reliable"] and state["observed_bytes"] > 0
    assert store.replace_day(engine, WEEK, [row()], now=NOW)["published"]
    with pytest.raises(ValueError, match="immutable"):
        store.configure(engine, start_week=WEEK + timedelta(days=7), contracts=contracts())
    with pytest.raises(ValueError):
        store.configure(engine, start_week=WEEK + timedelta(days=1), contracts=contracts())


def test_dry_run_only_selects_no_guard_initialization(engine):
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lstrip().upper())

    event.listen(engine, "before_cursor_execute", capture)
    assert store.configure(engine, start_week=WEEK, contracts=contracts(), dry_run=True)["dry_run"]
    store.maintain(engine, now=NOW, dry_run=True)
    store.save_observations(engine, [observation()], dry_run=True)
    assert store.load_state(engine) == {}
    assert all(statement.startswith("SELECT") for statement in statements)
    event.remove(engine, "before_cursor_execute", capture)
    ready(engine)
    statements.clear()
    event.listen(engine, "before_cursor_execute", capture)
    store.replace_day(engine, WEEK, [row()], now=NOW, dry_run=True)
    assert all(statement.startswith("SELECT") for statement in statements)
    assert not rows(engine, daily)


def test_atomic_replace_deletes_disappeared_groups_and_rolls_back(engine, monkeypatch):
    ready(engine)
    store.replace_day(engine, WEEK, [row(), row(scope="job")], now=NOW)
    assert store.load_state(engine)["retained_rows"] == 2
    moved = row(scope="job", key="reclassified-job")
    store.replace_day(engine, WEEK, [row(used=1.5), moved], now=NOW)
    assert len(rows(engine, daily)) == 2
    assert row(scope="job")["resource_key"] not in {
        item["resource_key"] for item in rows(engine, daily)
    }
    assert (
        next(item for item in rows(engine, daily) if item["scope"] == "account_class")[
            "metrics_json"
        ]["cpu"]["avg"]
        == 75
    )
    assert store.load_state(engine)["retained_rows"] == 2
    original = store._refresh_week

    def fail(*args, **kwargs):
        raise RuntimeError("weekly publish crash")

    monkeypatch.setattr(store, "_refresh_week", fail)
    with pytest.raises(RuntimeError, match="crash"):
        store.replace_day(engine, WEEK, [row(), moved], now=NOW)
    assert len(rows(engine, daily)) == 2
    assert (
        next(item for item in rows(engine, daily) if item["scope"] == "account_class")[
            "metrics_json"
        ]["cpu"]["avg"]
        == 75
    )
    assert store.load_state(engine)["retained_rows"] == 2
    monkeypatch.setattr(store, "_refresh_week", original)
    with pytest.raises(IntegrityError):
        store.replace_day(engine, WEEK, [row(), row(), moved], now=NOW)
    assert len(rows(engine, daily)) == 2


def test_weekly_weighted_scope_and_late_retry_checkpoint(engine):
    expected = contracts()
    expected["cvm"]["cpu"]["verified_horizon_seconds"] = 4 * 86400
    ready(engine, expected=expected)
    store.replace_day(
        engine,
        WEEK,
        [row(used=1.8, capacity=2), row(scope="resource", used=100, capacity=100)],
        now=NOW,
    )
    next_day = WEEK + timedelta(days=1)
    tomorrow = NOW + timedelta(days=1)
    store.maintain(engine, now=tomorrow)
    store.replace_day(
        engine, next_day, [row(next_day, used=3.2, capacity=32, peak=40)], now=tomorrow
    )
    result = rows(engine, weekly)[0]
    assert result["values_json"]["avg"] == pytest.approx(100 * 5 / 34)
    assert result["values_json"]["peak"] == 70
    # Independent checkpoint without another day publication.
    checkpoint = policy.day_start(WEEK + timedelta(days=9))
    store.maintain(engine, now=checkpoint)
    result = rows(engine, weekly)[0]
    assert result["quality_checkpoint_at"] == checkpoint
    assert result["status"] == "provisional"
    # Previous week's Sunday remains within this test's verified four-day source horizon.
    sunday = WEEK + timedelta(days=6)
    store.replace_day(
        engine,
        sunday,
        [row(sunday, used=0, peak=20, cutoff=policy.day_start(sunday + timedelta(days=1)))],
        now=checkpoint,
    )
    result = rows(engine, weekly)[0]
    assert result["values_json"]["observed_days"] == 3
    assert result["quality_checkpoint_at"] == checkpoint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("period", 300),
        ("source_stat", "max"),
        ("unit", "MiBytes"),
        ("denominator", "allocatable"),
        ("semantic_version", "cache-v2"),
    ],
)
def test_contract_mismatch_keeps_trusted_day_stale(engine, field, value):
    ready(engine)
    store.replace_day(engine, WEEK, [row()], now=NOW)
    bad = row(used=0)
    bad["metrics_json"]["cpu"]["contract"][field] = value
    result = store.replace_day(engine, WEEK, [bad], now=NOW)
    assert not result["published"]
    fact = rows(engine, daily)[0]
    assert fact["status"] == "stale" and fact["metrics_json"]["cpu"]["avg"] == 50
    assert "stale" in rows(engine, weekly)[0]["values_json"]["quality"]
    assert store.load_state(engine)["retained_rows"] == 1


def test_source_gap_retains_day_and_does_not_renew_expiry(engine):
    ready(engine)
    store.replace_day(engine, WEEK, [row()], now=NOW)
    original_expiry = rows(engine, daily)[0]["expires_at"]
    late = NOW + timedelta(days=4)
    store.maintain(engine, now=late)
    assert not store.replace_day(engine, WEEK, [row(used=0)], now=late)["published"]
    fact = rows(engine, daily)[0]
    assert fact["expires_at"] == original_expiry and fact["status"] == "stale"
    assert not store.replace_day(engine, WEEK, [], now=late)["published"]
    assert len(rows(engine, daily)) == 1


def test_observations_splits_requests_deduplicates_and_bounds_evidence(engine):
    ready(engine)
    assert store.save_observations(engine, [observation()], now=NOW)["rows"] == 1
    later = NOW + timedelta(minutes=5)
    assert store.save_observations(engine, [observation(later)], now=later)["rows"] == 0
    assert store.save_observations(engine, [observation(later, cpu=4)], now=later)["rows"] == 1
    facts = rows(engine, observations)
    assert len(facts) == 2 and facts[0]["observed_until"] == later
    assert facts[0]["expires_at"] == facts[1]["expires_at"] == policy.expires_at(WEEK)
    assert store.load_state(engine)["retained_rows"] == 2
    assert len(store.read_observations(engine, NOW, later + timedelta(seconds=1), "pod")) == 2
    for attrs in ({"author": "forbidden"}, {"repo": "x" * 9000}, {"cpu_request": -1}):
        bad = observation(later)
        bad["attributes_json"] = attrs
        with pytest.raises(ValueError):
            store.save_observations(engine, [bad], now=later)
    with pytest.raises(ValueError, match="out-of-order"):
        store.save_observations(engine, [observation()], now=later)
    with pytest.raises(ValueError):
        store.read_observations(engine, NOW, NOW + timedelta(days=15))
    with pytest.raises(ValueError):
        store.read_observations(engine, NOW, later, "disk")


def test_six_weeks_fixed_workset_and_missing_scopes_inactive_cleanup(engine):
    expected = contracts()
    expected["unobserved"] = deepcopy(expected["cvm"])
    ready(engine, expected=expected)
    for n in range(6):
        day, now = WEEK + timedelta(weeks=n), NOW + timedelta(weeks=n)
        store.maintain(engine, now=now)
        store.replace_day(engine, day, [row(day, cutoff=now)], now=now)
        store.save_observations(engine, [observation(now)], now=now)
        assert len(rows(engine, daily)) <= 2
        assert len(rows(engine, observations)) <= 2
        assert store.load_state(engine)["retained_rows"] <= 4
    store.configure(engine, start_week=WEEK, contracts=expected, enabled=False)
    final = NOW + timedelta(weeks=8)
    result = store.maintain(engine, now=final, batch_size=1)
    assert not rows(engine, daily) and not rows(engine, observations)
    assert result["state"]["retained_rows"] == 0
    assert result["state"]["finalized_through_week"] == (WEEK + timedelta(weeks=6)).isoformat()
    history = rows(engine, weekly)
    assert len([r for r in history if r["week_start"] < WEEK + timedelta(weeks=6)]) == 12
    assert all(
        r["status"] == "final_missing" and r["values_json"]["avg"] is None
        for r in history
        if r["resource_class"] == "unobserved"
    )
    with pytest.raises(ValueError):
        store.save_observations(engine, [observation()], now=final)


def test_complete_week_and_zero_requests_unknown_usage(engine):
    ready(engine)
    for n in range(7):
        day = WEEK + timedelta(days=n)
        end = policy.day_start(day + timedelta(days=1))
        store.maintain(engine, now=end)
        consumer = row(day, scope="job", capacity=0, complete=True, cutoff=end)
        assert store.replace_day(
            engine, day, [row(day, complete=True, cutoff=end), consumer], now=end
        )["published"]
    store.maintain(engine, now=policy.expires_at(WEEK))
    result = rows(engine, weekly)[0]
    assert result["status"] == "final_complete"
    assert result["values_json"]["avg"] == 50
    assert result["values_json"]["coverage"] == 1
    assert not rows(engine, daily)


def test_missing_whole_week_checkpoint_and_calendar_fence(engine):
    ready(engine)
    checkpoint = policy.day_start(WEEK + timedelta(days=9))
    store.maintain(engine, now=checkpoint)
    assert rows(engine, weekly)[0]["quality_checkpoint_at"] == checkpoint
    assert rows(engine, weekly)[0]["values_json"]["avg"] is None
    expiry = policy.expires_at(WEEK)
    store.maintain(engine, now=expiry)
    assert rows(engine, weekly)[0]["status"] == "final_missing"
    # An old worker's stale supplied clock cannot bypass the closed watermark.
    with pytest.raises(ValueError, match="expired, closed"):
        store.replace_day(engine, WEEK, [row()], now=expiry - timedelta(seconds=1))


def test_cleanup_batches_committed_restart_and_daily_before_evidence(engine):
    ready(engine)
    store.replace_day(engine, WEEK, [row(), row(scope="job"), row(scope="resource")], now=NOW)
    store.save_observations(engine, [observation()], now=NOW)
    expiry = policy.expires_at(WEEK)
    calls = []

    def crash_second(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("DELETE FROM cost_utilization_"):
            calls.append(statement)
            if len(calls) == 2:
                raise RuntimeError("simulated process crash")

    event.listen(engine, "before_cursor_execute", crash_second)
    with pytest.raises(RuntimeError, match="crash"):
        store.maintain(engine, now=expiry, batch_size=1)
    event.remove(engine, "before_cursor_execute", crash_second)
    assert len(rows(engine, daily)) == 2  # first delete really committed
    assert len(rows(engine, observations)) == 1
    assert store.load_state(engine)["retained_rows"] == 3
    limited = store.maintain(engine, now=expiry, batch_size=1, max_rows=2)
    assert limited["deleted_rows"] == 2 and limited["state"]["paused"]
    assert not rows(engine, daily) and len(rows(engine, observations)) == 1
    assert store.maintain(engine, now=expiry, batch_size=1)["deleted_rows"] == 1
    assert store.maintain(engine, now=expiry)["deleted_rows"] == 0
    assert store.load_state(engine)["retained_rows"] == 0


def test_shared_finalizer_writer_fence(engine, monkeypatch):
    ready(engine)
    store.replace_day(engine, WEEK, [row()], now=NOW)
    expiry = policy.expires_at(WEEK)
    # Ready maintenance state at D-epsilon, then let the finalizer own the guard.
    store.maintain(engine, now=expiry - timedelta(seconds=1))
    started = threading.Event()
    original = store._refresh_week
    future = None

    def stale_writer():
        started.set()
        return store.replace_day(engine, WEEK, [row()], now=expiry - timedelta(seconds=1))

    with ThreadPoolExecutor(max_workers=1) as executor:

        def fenced(*args, **kwargs):
            nonlocal future
            if kwargs.get("final") and future is None:
                future = executor.submit(stale_writer)
                assert started.wait(2)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "_refresh_week", fenced)
        store.maintain(engine, now=expiry)
        with pytest.raises(ValueError, match="expired, closed"):
            future.result(timeout=5)
    assert rows(engine, weekly)[0]["values_json"]["avg"] == 50


@pytest.mark.parametrize("table", [daily, observations])
def test_reconcile_counts_both_tables_drift_pauses_then_recovers(engine, table):
    ready(engine)
    if table is daily:
        store.replace_day(engine, WEEK, [row()], now=NOW)
    else:
        store.save_observations(engine, [observation()], now=NOW)
    set_state(engine, retained_rows=0, reconciled_at=(NOW - timedelta(days=1)).isoformat())
    result = store.maintain(engine, now=NOW)
    assert result["state"]["retained_rows"] == 1
    assert result["state"]["paused"] and "count_drift" in result["state"]["pause_reasons"]
    assert not store.maintain(engine, now=NOW)["state"]["paused"]


@pytest.mark.parametrize("table", [daily, observations])
def test_byte_budget_uses_data_and_all_indexes_of_both_tables(engine, table, monkeypatch):
    ready(engine)
    with engine.connect() as connection:
        before = store._observed_bytes(connection)
    if table is daily:
        store.replace_day(
            engine, WEEK, [row(key=str(n), scope="resource") for n in range(50)], now=NOW
        )
    else:
        store.save_observations(
            engine, [observation(resource_id=str(n)) for n in range(50)], now=NOW
        )
    with engine.connect() as connection:
        after = store._observed_bytes(connection)
        measured = connection.execute(
            text(
                "SELECT SUM(pgsize) FROM dbstat WHERE name IN ("
                "SELECT name FROM sqlite_master WHERE tbl_name IN "
                "('cost_utilization_daily','cost_utilization_resource_observation'))"
            )
        ).scalar()
    assert after == measured and after > before
    monkeypatch.setattr(store, "_observed_bytes", lambda _connection: policy.BYTE_LIMIT)
    assert store.maintain(engine, now=NOW)["state"]["paused"]
    with pytest.raises(ValueError, match="blocked"):
        store.replace_day(engine, WEEK, [row()], now=NOW)
    monkeypatch.setattr(store, "_observed_bytes", lambda _connection: policy.BYTE_WARNING)
    assert store.maintain(engine, now=NOW)["state"]["paused"]
    monkeypatch.setattr(store, "_observed_bytes", lambda _connection: 1)
    assert not store.maintain(engine, now=NOW)["state"]["paused"]


def test_empty_account_is_missing_not_zero_and_consumer_only_has_no_weekly_scope(engine):
    expected = contracts()
    expected["tke_unknown"] = deepcopy(expected["cvm"])
    expected["tke_unknown"]["cpu"]["consumer_only"] = True
    ready(engine, expected=expected)
    assert not store.replace_day(engine, WEEK, [], now=NOW)["published"]
    assert not rows(engine, daily)
    consumer = row(scope="job")
    consumer["resource_class"] = "tke_unknown"
    assert store.replace_day(engine, WEEK, [consumer], now=NOW)["published"]
    assert {r["resource_class"] for r in rows(engine, weekly)} == {"cvm"}
    invalid = row()
    invalid["resource_class"] = "tke_unknown"
    assert not store.replace_day(engine, WEEK, [invalid], now=NOW)["published"]


def test_evidence_gap_splits_without_backdating_and_new_week_requires_new_observation(engine):
    ready(engine)
    store.save_observations(engine, [observation()], now=NOW)
    later = NOW + timedelta(hours=2)
    store.save_observations(engine, [observation(later)], now=later)
    facts = rows(engine, observations)
    assert len(facts) == 2
    assert facts[0]["observed_until"] == NOW
    assert facts[1]["observed_from"] == later
    next_week = NOW + timedelta(days=7)
    store.maintain(engine, now=next_week)
    assert len(rows(engine, observations)) == 2  # no automatic carry-forward
    store.save_observations(engine, [observation(next_week)], now=next_week)
    assert len(rows(engine, observations)) == 3
    assert rows(engine, observations)[2]["week_start"] == WEEK + timedelta(days=7)
    assert rows(engine, observations)[0]["expires_at"] == policy.expires_at(WEEK)


def test_finalization_failure_fallback_does_not_hold_expired_detail(engine, monkeypatch):
    ready(engine)
    store.replace_day(engine, WEEK, [row()], now=NOW)

    def invalid(*_args, **_kwargs):
        raise ValueError("invalid bounded account_class input")

    monkeypatch.setattr(store, "_refresh_week", invalid)
    expiry = policy.expires_at(WEEK)
    result = store.maintain(engine, now=expiry)
    assert result["deleted_rows"] == 1
    assert not rows(engine, daily)
    final = rows(engine, weekly)[0]
    assert final["status"] == "final_incomplete"
    assert final["values_json"]["avg"] == 50
    assert "finalization_failed" in final["values_json"]["quality"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("used_integral", -1),
        ("capacity_integral", float("inf")),
        ("coverage", 1.5),
        ("peak", -1),
        ("quality", [5]),
        ("raw_response", {"labels": "not allowed"}),
    ],
)
def test_invalid_metric_bodies_rejected_without_zero_overwrite(engine, field, value):
    ready(engine)
    fact = row()
    fact["metrics_json"]["cpu"][field] = value
    assert not store.replace_day(engine, WEEK, [fact], now=NOW)["published"]
    assert not rows(engine, daily)


def test_identity_and_interval_validation(engine):
    ready(engine)
    for overrides in (
        {"scope": "person"},
        {"vendor": "gcp"},
        {"resource_key": "short"},
        {"cutoff": NOW + timedelta(hours=1)},
        {"status": "invented"},
        {"evidence_cutoff": NOW + timedelta(hours=1)},
    ):
        assert not store.replace_day(engine, WEEK, [dict(row(), **overrides)], now=NOW)["published"]
    for overrides in (
        {"resource_type": "disk"},
        {"resource_id": "x" * 256},
        {"vendor": "gcp"},
        {"observed_from": NOW + timedelta(hours=1)},
        {"observed_until": NOW - timedelta(hours=1)},
        {"attributes_json": {"cpu_request": -1}},
    ):
        with pytest.raises(ValueError):
            store.save_observations(engine, [observation(**overrides)], now=NOW)
    with pytest.raises(ValueError, match="1000"):
        store.save_observations(engine, [observation()] * 1001, now=NOW)
    assert store.maintain(engine, now=NOW, dry_run=True)["retained_rows"] == 0


def test_time_budget_rolls_back_publication_and_unconfigured_maintenance(engine, monkeypatch):
    assert not store.maintain(engine, now=NOW)["configured"]
    ready(engine)
    monkeypatch.setattr(store, "MAX_TRANSACTION_SECONDS", -1)
    with pytest.raises(ValueError, match="time ceiling"):
        store.replace_day(engine, WEEK, [row()], now=NOW)
    assert not rows(engine, daily) and not rows(engine, weekly)
    assert store.load_state(engine)["retained_rows"] == 0


def test_stop_week_is_explicit_inclusive_and_not_reset_on_reenable(engine):
    ready(engine)
    result = store.configure(
        engine, start_week=WEEK, contracts=contracts(), enabled=False, stop_week=WEEK, now=NOW
    )
    assert result["collection_stop_week"] == WEEK.isoformat()
    result = store.configure(engine, start_week=WEEK, contracts=contracts(), enabled=True, now=NOW)
    assert result["collection_stop_week"] == WEEK.isoformat()
    later = NOW + timedelta(weeks=6)
    store.maintain(engine, now=later)
    assert len(rows(engine, weekly)) == 1
    assert store.load_state(engine)["finalized_through_week"] == WEEK.isoformat()
    with pytest.raises(ValueError, match="stop_week"):
        store.configure(
            engine, start_week=WEEK, contracts=contracts(), stop_week=WEEK - timedelta(days=7)
        )
    with pytest.raises(ValueError, match="stop_week"):
        store.configure(
            engine, start_week=WEEK, contracts=contracts(), stop_week=WEEK + timedelta(days=1)
        )


def test_tidb_stats_require_data_and_index_bytes_from_both_tables():
    class Result:
        def mappings(self):
            return self

        def one(self):
            return self.value

    class Dialect:
        name = "mysql"

    class Connection:
        dialect = Dialect()

        def execute(self, statement):
            assert "DATA_LENGTH + INDEX_LENGTH" in str(statement)
            assert "cost_utilization_daily" in str(statement)
            assert "cost_utilization_resource_observation" in str(statement)
            return result

    result = Result()
    result.value = {"n": 2, "measured_n": 2, "bytes": 1234}
    assert store._observed_bytes(Connection()) == 1234
    result.value["measured_n"] = 1
    with pytest.raises(ValueError, match="unreliable"):
        store._observed_bytes(Connection())
    with pytest.raises(ValueError, match="clock override"):
        store._clock(Connection(), NOW)


def test_warning_budget_does_not_pause_active_ingestion(engine, monkeypatch):
    ready(engine)
    monkeypatch.setattr(store, "_observed_bytes", lambda _connection: policy.BYTE_WARNING)
    result = store.maintain(engine, now=NOW)
    assert not result["state"]["paused"]
    assert "byte_budget" in result["state"]["warnings"]
    assert store.replace_day(engine, WEEK, [row()], now=NOW)["published"]


def test_weekly_resource_seconds_do_not_turn_missing_days_into_full_coverage(engine):
    ready(engine)
    fact = row()
    body = fact["metrics_json"]["cpu"]
    body["observed_seconds"] = body["expected_seconds"] = 10 * 86400
    store.replace_day(engine, WEEK, [fact], now=NOW)
    values = rows(engine, weekly)[0]["values_json"]
    assert values["expected_seconds"] == 10 * 86400
    assert values["coverage"] is None
    assert values["observed_coverage"] == 1
    assert "missing_days" in values["quality"]


def test_writer_budget_maintenance_outage_and_expiry_index(engine):
    ready(engine)
    set_state(engine, retained_rows=policy.ROW_LIMIT - 1)
    with pytest.raises(ValueError, match="row_budget"):
        store.save_observations(engine, [observation()], now=NOW)
    set_state(engine, retained_rows=0)
    with pytest.raises(ValueError, match="maintenance_stale"):
        store.replace_day(engine, WEEK, [row()], now=NOW + timedelta(hours=5))
    with engine.connect() as connection:
        plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM cost_utilization_daily "
                "WHERE expires_at <= :now ORDER BY expires_at,id LIMIT 1000"
            ),
            {"now": NOW},
        ).all()
    assert "ix_utilization_daily_expiry" in str(plan)
    for args in ({"batch_size": 1001}, {"max_rows": 50001}, {"max_seconds": 301}):
        with pytest.raises(ValueError):
            store.maintain(engine, **args)


@pytest.mark.parametrize(
    ("quality", "status"),
    [
        (["missing_points", "capacity_changed", "zero_requests"], "final_complete"),
        *[([flag], "final_incomplete") for flag in store.HARD_QUALITY - {"missing_days"}],
    ],
)
def test_weekly_numeric_coverage_95_percent_and_hard_flags(engine, quality, status):
    ready(engine)
    for n in range(7):
        day = WEEK + timedelta(days=n)
        end = policy.day_start(day + timedelta(days=1))
        store.maintain(engine, now=end)
        fact = row(day, complete=True, cutoff=end)
        if n == 0:
            body = fact["metrics_json"]["cpu"]
            body["observed_seconds"] -= 60
            body["capacity_integral"] -= 120
            body["coverage"] = None  # Legacy fixture NULL must not erase the numeric ratio.
            body["unknown_seconds"] = 60 if "unknown_capacity" in quality else 0
            body["zero_capacity_seconds"] = 60 if "zero_requests" in quality else 0
            body["quality"] = quality
        assert store.replace_day(engine, day, [fact], now=end)["published"]
    store.maintain(engine, now=policy.expires_at(WEEK))
    result = rows(engine, weekly)[0]
    values = result["values_json"]
    assert result["status"] == status
    assert values["coverage"] == pytest.approx((7 * 86400 - 60) / (7 * 86400))
    assert values["observed_coverage"] == values["coverage"]
    assert values["unknown_seconds"] == (60 if "unknown_capacity" in quality else 0)
    assert values["zero_capacity_seconds"] == (60 if "zero_requests" in quality else 0)


@pytest.mark.parametrize(("ratio", "status"), [(0.9, "final_incomplete"), (0.95, "final_complete")])
def test_numeric_coverage_threshold_even_without_flags(engine, ratio, status):
    ready(engine)
    for n in range(7):
        day = WEEK + timedelta(days=n)
        end = policy.day_start(day + timedelta(days=1))
        store.maintain(engine, now=end)
        fact = row(day, complete=True, cutoff=end)
        body = fact["metrics_json"]["cpu"]
        body["observed_seconds"] *= ratio
        body["coverage"] = 1  # Recompute from sufficient stats, not the caller's ratio.
        assert store.replace_day(engine, day, [fact], now=end)["published"]
    store.maintain(engine, now=policy.expires_at(WEEK))
    result = rows(engine, weekly)[0]
    assert result["values_json"]["coverage"] == ratio
    assert result["status"] == status


def test_known_zero_requests_preserve_usage_and_legacy_seconds_default(engine):
    ready(engine)
    fact = row(scope="job", capacity=0)
    body = fact["metrics_json"]["cpu"]
    body["quality"] = ["zero_requests"]
    body["zero_capacity_seconds"] = body["expected_seconds"]
    assert store.replace_day(engine, WEEK, [row(), fact], now=NOW)["published"]
    persisted = next(r for r in rows(engine, daily) if r["scope"] == "job")["metrics_json"]["cpu"]
    assert persisted["avg"] is None and persisted["coverage"] == 1
    assert persisted["used_integral"] == persisted["total_used_integral"] == 43200
    assert persisted["observed_seconds"] == persisted["zero_capacity_seconds"] == 43200
    assert "unknown_capacity" not in persisted["quality"]
    assert rows(engine, weekly)[0]["values_json"]["unknown_seconds"] == 0
    assert rows(engine, weekly)[0]["values_json"]["zero_capacity_seconds"] == 0


@pytest.mark.parametrize("field", ["unknown_seconds", "zero_capacity_seconds"])
@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "0", 43201])
def test_optional_entity_seconds_are_bounded(engine, field, value):
    ready(engine)
    fact = row()
    fact["metrics_json"]["cpu"][field] = value
    assert not store.replace_day(engine, WEEK, [fact], now=NOW)["published"]
    assert not rows(engine, daily)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verified_horizon_seconds", 0),
        ("verified_horizon_seconds", float("inf")),
        ("verified_horizon_seconds", float("nan")),
        ("safety_margin_seconds", -1),
        ("safety_margin_seconds", 3 * 86400),
        ("timestamp_alignment_verified", False),
    ],
)
def test_configure_rejects_invalid_source_window_even_when_disabled(engine, field, value):
    expected = contracts()
    expected["cvm"]["cpu"][field] = value
    with pytest.raises(ValueError):
        store.configure(engine, start_week=WEEK, contracts=expected, now=NOW)
    assert store.load_state(engine) == {}


def test_configure_rejects_bad_scale_in_source_or_scope_contract(engine):
    for scope in (None, "job"):
        expected = contracts()
        target = expected["cvm"]["cpu"]
        target = target["contract"] if scope is None else target["scope_contracts"][scope]
        target["source_scale"] = 0
        with pytest.raises(ValueError, match="source_scale"):
            store.configure(engine, start_week=WEEK, contracts=expected, now=NOW)
    assert store.load_state(engine) == {}


SCOPE_HASH = "a" * 64


def empty_contracts():
    expected = contracts()
    expected["cvm"]["cpu"]["contract"]["source_scope_hash"] = SCOPE_HASH
    return expected


def empty_proof(day=WEEK, *, cutoff=NOW, **changes):
    return {
        "vendor": "tencent",
        "account_id": "100050658403",
        "usage_date": day.isoformat(),
        "cutoff": cutoff.isoformat() + "Z",
        "resource_classes": ["cvm"],
        "source_scope_hash": SCOPE_HASH,
        "evidence_reference": "operator-proof:inventory/123",
        "verified_at": cutoff.isoformat() + "Z",
        "confirmed_empty": True,
        "source_quality": [],
        **changes,
    }


def test_verified_empty_seven_days_final_complete_avg_null_not_inferred_zero(engine):
    ready(engine, expected=empty_contracts())
    for n in range(7):
        day = WEEK + timedelta(days=n)
        end = policy.day_start(day + timedelta(days=1))
        store.maintain(engine, now=end)
        proof = empty_proof(day, cutoff=end)
        assert store.replace_day(engine, day, [], verified_empty=proof, now=end)["published"]
        fact = next(r for r in rows(engine, daily) if r["usage_date"] == day)
        assert fact["scope"] == "account_class" and fact["status"] == "complete"
        assert fact["resource_key"] == hashlib.sha256(b'["account_class","cvm"]').hexdigest()
        body = fact["metrics_json"]["cpu"]
        assert body["avg"] is body["peak"] is body["latest"] is None
        assert (
            body["expected_seconds"]
            == body["unknown_seconds"]
            == body["zero_capacity_seconds"]
            == 0
        )
        assert body["coverage"] == 1
        digest = hashlib.sha256(
            json.dumps(proof, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        assert body["quality"] == ["verified_empty", "proof_sha256:" + digest]
        assert "operator-proof" not in json.dumps(fact["metrics_json"])
    store.maintain(engine, now=policy.expires_at(WEEK))
    summary = next(r for r in rows(engine, weekly) if r["week_start"] == WEEK)
    assert summary["status"] == "final_complete"
    assert summary["values_json"]["avg"] is summary["values_json"]["peak"] is None
    assert summary["values_json"]["coverage"] == 1
    assert summary["values_json"]["observed_days"] == summary["values_json"]["expected_days"] == 7
    assert not rows(engine, daily) and not rows(engine, observations)
    assert store.load_state(engine)["retained_rows"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"vendor": "gcp"},
        {"account_id": "other"},
        {"usage_date": "2026-11-17"},
        {"confirmed_empty": False},
        {"resource_classes": []},
        {"resource_classes": ["cvm", "cvm"]},
        {"resource_classes": ["not-configured"]},
        {"resource_classes": "cvm"},
        {"source_scope_hash": "b" * 64},
        {"source_scope_hash": "not-sha256"},
        {"evidence_reference": ""},
        {"evidence_reference": "x" * 256},
        {"evidence_reference": "x" * 9000},
        {"cutoff": (NOW + timedelta(seconds=1)).isoformat() + "Z"},
        {"cutoff": policy.day_start(WEEK).isoformat() + "Z"},
        {"verified_at": (NOW + timedelta(seconds=1)).isoformat() + "Z"},
        {"verified_at": (NOW - timedelta(seconds=1)).isoformat() + "Z"},
        {"verified_at": NOW.isoformat()},
        {"cutoff": "2026-11-16T12:00:00+08:00"},
        {"source_quality": ["missing_points"]},
        {"author": "not-whitelisted"},
        {"raw_response": "x" * 9000},
    ],
)
def test_verified_empty_invalid_proof_retains_trusted_day_stale(engine, changes):
    expected = empty_contracts()
    ready(engine, expected=expected)
    fact = row()
    fact["metrics_json"]["cpu"]["contract"] = expected["cvm"]["cpu"]["contract"]
    assert store.replace_day(engine, WEEK, [fact], now=NOW)["published"]
    result = store.replace_day(
        engine, WEEK, [], verified_empty=dict(empty_proof(), **changes), now=NOW
    )
    assert not result["published"]
    persisted = rows(engine, daily)[0]
    assert persisted["status"] == "stale" and persisted["metrics_json"]["cpu"]["avg"] == 50
    assert store.load_state(engine)["retained_rows"] == 1


@pytest.mark.parametrize("scope", ["account_class", "resource"])
def test_verified_empty_rejects_contradicting_physical_rows(engine, scope):
    expected = empty_contracts()
    ready(engine, expected=expected)
    fact = row(scope=scope)
    fact["metrics_json"]["cpu"]["contract"] = expected["cvm"]["cpu"]["contract"]
    result = store.replace_day(engine, WEEK, [fact], verified_empty=empty_proof(), now=NOW)
    assert not result["published"] and "contradicts" in result["reason"]
    assert not rows(engine, daily)


def test_verified_empty_requires_every_metric_scope_hash_and_physical_class(engine):
    expected = empty_contracts()
    expected["cvm"]["memory"] = deepcopy(expected["cvm"]["cpu"])
    expected["cvm"]["memory"]["contract"].update(normalized_unit="bytes", metric="MemUsage")
    expected["cvm"]["memory"]["scope_contracts"] = {}
    expected["cvm"]["memory"]["contract"]["source_scope_hash"] = "b" * 64
    expected["consumer"] = deepcopy(expected["cvm"])
    for entry in expected["consumer"].values():
        entry["consumer_only"] = True
    ready(engine, expected=expected)
    result = store.replace_day(engine, WEEK, [], verified_empty=empty_proof(), now=NOW)
    assert not result["published"] and "scope hash" in result["reason"]
    result = store.replace_day(
        engine, WEEK, [], verified_empty=empty_proof(resource_classes=["consumer"]), now=NOW
    )
    assert not result["published"] and "physical inventory" in result["reason"]


def test_verified_empty_guard_horizon_partial_and_historical_cutoff(engine):
    expected = empty_contracts()
    ready(engine, expected=expected)
    proof = empty_proof()
    set_state(engine, enabled=False)
    with pytest.raises(ValueError, match="blocked"):
        store.replace_day(engine, WEEK, [], verified_empty=proof, now=NOW)
    set_state(engine, enabled=True)
    assert store.replace_day(engine, WEEK, [], verified_empty=proof, now=NOW)["published"]
    assert rows(engine, daily)[0]["status"] == "partial"
    tomorrow = NOW + timedelta(days=1)
    store.maintain(engine, now=tomorrow)
    result = store.replace_day(engine, WEEK, [], verified_empty=proof, now=tomorrow)
    assert not result["published"] and "cutoff" in result["reason"]
    end = policy.day_start(WEEK + timedelta(days=1))
    proof = empty_proof(cutoff=end)
    assert store.replace_day(engine, WEEK, [], verified_empty=proof, now=tomorrow)["published"]
    late = NOW + timedelta(days=4)
    store.maintain(engine, now=late)
    result = store.replace_day(engine, WEEK, [], verified_empty=proof, now=late)
    assert not result["published"] and "horizon" in result["reason"]
    expiry = policy.expires_at(WEEK)
    store.maintain(engine, now=expiry)
    with pytest.raises(ValueError, match="expired, closed"):
        store.replace_day(engine, WEEK, [], verified_empty=proof, now=expiry)


def test_verified_empty_dry_run_select_only_and_no_stale_update(engine):
    ready(engine, expected=empty_contracts())
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lstrip().upper())

    event.listen(engine, "before_cursor_execute", capture)
    result = store.replace_day(
        engine, WEEK, [], verified_empty=empty_proof(), now=NOW, dry_run=True
    )
    assert result == {"dry_run": True, "rows": 1}
    with pytest.raises(ValueError):
        store.replace_day(
            engine,
            WEEK,
            [],
            verified_empty=empty_proof(source_scope_hash="b" * 64),
            now=NOW,
            dry_run=True,
        )
    event.remove(engine, "before_cursor_execute", capture)
    assert all(statement.startswith("SELECT") for statement in statements)
    assert not rows(engine, daily) and not rows(engine, weekly)
    assert store.load_state(engine)["retained_rows"] == 0


def test_verified_empty_source_quality_hard_flags_survive_rollup(engine):
    ready(engine, expected=empty_contracts())
    proof = empty_proof(source_quality=["unmapped_node_inventory", "unmapped_physical_source"])
    assert store.replace_day(engine, WEEK, [], verified_empty=proof, now=NOW)["published"]
    values = rows(engine, weekly)[0]["values_json"]
    assert values["observed_coverage"] == 1 and values["coverage"] is None
    assert {"unmapped_node_inventory", "unmapped_physical_source", "partial"} <= set(
        values["quality"]
    )


def test_seven_unproved_empty_days_remain_final_missing(engine):
    ready(engine, expected=empty_contracts())
    for n in range(7):
        day = WEEK + timedelta(days=n)
        end = policy.day_start(day + timedelta(days=1))
        store.maintain(engine, now=end)
        assert not store.replace_day(engine, day, [], now=end)["published"]
    store.maintain(engine, now=policy.expires_at(WEEK))
    result = rows(engine, weekly)[0]
    assert result["status"] == "final_missing"
    assert result["values_json"]["coverage"] is result["values_json"]["avg"] is None
    assert result["values_json"]["observed_days"] == 0


@pytest.mark.parametrize("quality", [["verified_empty"], ["proof_sha256:" + "a" * 64]])
def test_verified_empty_flags_cannot_be_forged_in_ordinary_rows(engine, quality):
    ready(engine)
    fact = row(capacity=0, used=0)
    fact["metrics_json"]["cpu"]["quality"] = quality
    assert not store.replace_day(engine, WEEK, [fact], now=NOW)["published"]
    assert not rows(engine, daily)


def test_verified_empty_materializes_all_metrics_only_account_class_and_can_keep_consumers(engine):
    expected = empty_contracts()
    expected["cvm"]["memory"] = deepcopy(expected["cvm"]["cpu"])
    expected["cvm"]["memory"]["contract"].update(normalized_unit="bytes", metric="MemUsage")
    expected["cvm"]["memory"]["scope_contracts"] = {}
    ready(engine, expected=expected)
    assert store.replace_day(
        engine, WEEK, [row(scope="job")], verified_empty=empty_proof(), now=NOW
    )["published"]
    facts = rows(engine, daily)
    assert {r["scope"] for r in facts} == {"account_class", "job"}
    empty = next(r for r in facts if r["scope"] == "account_class")
    assert set(empty["metrics_json"]) == {"cpu", "memory"}
    assert all(
        body["avg"] is None and body["coverage"] == 1 for body in empty["metrics_json"].values()
    )
    assert store.load_state(engine)["retained_rows"] == 2


def test_verified_empty_generated_rows_obey_budget_and_fresh_precommit_fence(engine, monkeypatch):
    ready(engine, expected=empty_contracts())
    set_state(engine, retained_rows=policy.ROW_LIMIT - 1)
    with pytest.raises(ValueError, match="row_budget"):
        store.replace_day(engine, WEEK, [], verified_empty=empty_proof(), now=NOW)
    assert not rows(engine, daily)
    set_state(engine, retained_rows=0)
    expiry = policy.expires_at(WEEK)
    original_clock = store._clock
    calls = 0

    def crossing_clock(connection, now=None):
        nonlocal calls
        calls += 1
        return original_clock(connection, now) if calls == 1 else expiry

    monkeypatch.setattr(store, "_clock", crossing_clock)
    with pytest.raises(ValueError, match="blocked|expired, closed"):
        store.replace_day(engine, WEEK, [], verified_empty=empty_proof(), now=NOW)
    assert not rows(engine, daily) and not rows(engine, weekly)
    assert store.load_state(engine)["retained_rows"] == 0


def test_verified_empty_dry_run_never_initializes_guard_and_allows_disabled_validation(engine):
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lstrip().upper())

    event.listen(engine, "before_cursor_execute", capture)
    with pytest.raises(ValueError, match="calendar"):
        store.replace_day(engine, WEEK, [], verified_empty=empty_proof(), now=NOW, dry_run=True)
    assert store.load_state(engine) == {}
    event.remove(engine, "before_cursor_execute", capture)
    assert all(statement.startswith("SELECT") for statement in statements)
    store.configure(engine, start_week=WEEK, contracts=empty_contracts(), now=NOW)
    assert (
        store.replace_day(engine, WEEK, [], verified_empty=empty_proof(), now=NOW, dry_run=True)[
            "rows"
        ]
        == 1
    )
    assert not rows(engine, daily)
    assert store.load_state(engine)["paused"]
