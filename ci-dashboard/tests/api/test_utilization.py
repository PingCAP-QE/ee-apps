from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from ci_dashboard.api.main import create_app
from ci_dashboard.api.queries import utilization as queries
from ci_dashboard.api.routes import utilization as routes

NOW = datetime(2026, 11, 24, 5, tzinfo=UTC)
FIELDS = (
    "used", "capacity", "total_used", "expected_capacity", "expected_seconds",
    "observed_seconds", "usage_seconds", "unknown_seconds", "zero_seconds",
)


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    stats = ",".join(f"{metric}_{field} REAL" for metric in ("cpu", "memory") for field in FIELDS)
    with engine.begin() as connection:
        connection.execute(text(f"""CREATE TABLE machine_utilization_daily (
            id INTEGER PRIMARY KEY, account_id TEXT, region TEXT, cluster_id TEXT,
            machine_id TEXT, usage_date DATE, resource_class TEXT, instance_type TEXT,
            cpu_cores REAL, memory_bytes REAL, cutoff DATETIME, updated_at DATETIME,
            quality_json JSON, {stats}, cpu_peak REAL, cpu_peak_at DATETIME,
            memory_peak REAL, memory_peak_at DATETIME,
            UNIQUE (account_id, region, cluster_id, machine_id, usage_date))"""))
        connection.execute(text(f"""CREATE TABLE workload_utilization_daily (
            id INTEGER PRIMARY KEY, account_id TEXT, usage_date DATE, group_key TEXT,
            labels_json JSON, cpu_request_cores REAL, memory_request_bytes REAL,
            cutoff DATETIME, updated_at DATETIME, expires_at DATETIME, quality_json JSON, {stats})"""))
        connection.execute(text("""CREATE TABLE pod_utilization_context (
            id INTEGER PRIMARY KEY, account_id TEXT, closed_at DATETIME, expires_at DATETIME,
            planned_until DATETIME, observed_until DATETIME)"""))
        connection.execute(text("""CREATE TABLE utilization_task (
            id TEXT PRIMARY KEY, account_id TEXT, status TEXT, deadline_at DATETIME,
            kind TEXT, window_start DATETIME)"""))
        connection.execute(text(
            "CREATE TABLE cost_job_state (job_name TEXT PRIMARY KEY, watermark_json JSON)"
        ))
    configure(engine)

    def no_legacy_reads(_conn, _cursor, statement, *_args):
        assert "cost_utilization_" not in statement, "V2 must never access legacy fact tables"

    event.listen(engine, "before_cursor_execute", no_legacy_reads)
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")
    monkeypatch.setattr(queries, "utc_now", lambda: NOW)
    app = create_app()
    app.dependency_overrides[routes.utilization_engine] = lambda: engine
    with TestClient(app) as client:
        yield client


def configure(engine, **extra):
    state = {
        "version": 2, "enabled": True,
        "contracts": {"cvm": {"cpu": {"contract": {"denominator": "physical"}}}},
        "scope": {"account_id": queries.ACCOUNT_ID,
                  "labels": ["repo", "job", "test_project", "test_suite", "environment", "project", "service"]},
        "maintenance_at": "2026-11-24T04:00:00", "paused": False,
        "pause_reasons": [], "counts_reliable": True, "retained_rows": 0,
        "observed_bytes": 0, "bytes_observed_at": "2026-11-24T04:00:00", **extra,
    }
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM cost_job_state"))
        connection.execute(text("INSERT INTO cost_job_state VALUES (:key,:state)"),
                           {"key": queries.STATE_KEY, "state": json.dumps(state)})


def metric(**extra):
    return {"used": 50, "capacity": 200, "total_used": 70, "expected_capacity": 300,
            "expected_seconds": 60, "observed_seconds": 40, "usage_seconds": 60,
            "unknown_seconds": 10, "zero_seconds": 10, **extra}


def insert(engine, kind="machine", cpu=None, memory=None, **extra):
    row = {
        "account_id": queries.ACCOUNT_ID, "usage_date": "2026-11-24",
        "cutoff": "2026-11-24 04:00:00", "updated_at": "2026-11-24 04:01:00",
        "quality_json": "[]",
    }
    for prefix, values in (("cpu", cpu), ("memory", memory)):
        row.update({f"{prefix}_{key}": value for key, value in (values or metric()).items()})
    if kind == "machine":
        row.update(region="ap-beijing", cluster_id="", machine_id="ins-example", resource_class="cvm",
                   instance_type="S5.LARGE8", cpu_cores=4, memory_bytes=8 * 1024**3,
                   cpu_peak=143.12, cpu_peak_at="2026-11-24 03:00:00", memory_peak=None,
                   memory_peak_at=None)
    else:
        row.update(group_key="anonymous-label-snapshot", labels_json=json.dumps({"service": "build"}),
                   cpu_request_cores=2, memory_request_bytes=None, expires_at="2026-11-29 16:00:00")
    row.update(extra)
    with engine.begin() as connection:
        connection.execute(text(
            f"INSERT INTO {kind}_utilization_daily ({','.join(row)}) "
            f"VALUES ({','.join(':' + key for key in row)})"
        ), row)


def test_disabled_needs_no_database_and_weekly_is_retired(monkeypatch):
    monkeypatch.delenv("CI_DASHBOARD_UTILIZATION_ENABLED", raising=False)
    monkeypatch.setattr(routes, "get_engine", lambda: pytest.fail("must not build engine"))
    with TestClient(create_app()) as client:
        for path in ("status", "daily"):
            payload = client.get(f"/api/v1/utilization/{path}").json()
            assert payload["enabled"] is False and payload["available"] is False
            assert payload["reason"] == "disabled"
        assert client.get("/api/v1/utilization/weekly").json()["reason"] == "not_supported"
    assert queries.get_daily(None)["reason"] == "disabled"
    assert queries.get_status(None)["reason"] == "disabled"
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/utilization/weekly").json()["reason"] == "not_supported"


@pytest.mark.parametrize("unsettled", ["task", "tail"])
def test_workload_coverage_is_partial_until_confirmed_windows_settle(client, engine, unsettled):
    full_window = metric(expected_seconds=600, observed_seconds=600,
                         expected_capacity=1200, capacity=1200, usage_seconds=600)
    insert(engine, "workload", cpu=full_window, memory=full_window)
    params = {"kind": "workload", "start_date": "2026-11-24", "end_date": "2026-11-24"}
    assert client.get("/api/v1/utilization/daily", params=params).json()["partial"] is False
    with engine.begin() as connection:
        if unsettled == "task":
            connection.execute(text(
                "INSERT INTO utilization_task (id,account_id,status,kind,window_start) "
                "VALUES ('older',:account,'pending','pod','2026-11-23 16:00:00.000000')"
            ), {"account": queries.ACCOUNT_ID})  # Start of the selected Beijing day.
        else:
            connection.execute(text(
                "INSERT INTO pod_utilization_context (account_id,planned_until,observed_until) "
                "VALUES (:account,'2026-11-23 16:00:00.000000','2026-11-23 16:10:00.000000')"
            ), {"account": queries.ACCOUNT_ID})
    result = client.get("/api/v1/utilization/daily", params=params).json()
    assert result["partial"] is True
    assert result["items"][0]["metrics"]["cpu"]["coverage"] == 1.0  # Settled windows only.
    other_day = {**params, "start_date": "2026-11-23", "end_date": "2026-11-23"}
    assert client.get("/api/v1/utilization/daily", params=other_day).json()["partial"] is False
    with engine.begin() as connection:
        if unsettled == "task":
            connection.execute(text("UPDATE utilization_task SET status='expired'"))
        else:
            connection.execute(text("UPDATE pod_utilization_context SET planned_until=observed_until"))
    assert client.get("/api/v1/utilization/daily", params=params).json()["partial"] is False


def test_machine_identity_physical_peak_and_derived_metrics(client, engine):
    insert(engine)
    insert(engine, account_id="other-account")
    insert(engine, machine_id="np-other", resource_class="tke_native")
    payload = client.get("/api/v1/utilization/daily", params={
        "kind": "machine", "machine_id": "ins-example", "region": "ap-beijing",
        "resource_class": "cvm",
    }).json()
    assert payload["available"] is True and payload["kind"] == "machine"
    assert len(payload["items"]) == 1
    row = payload["items"][0]
    assert row["machine_id"] == "ins-example" and row["region"] == "ap-beijing"
    assert row["cluster_id"] == "" and row["usage_date"] == "2026-11-24"
    assert row["instance_type"] == "S5.LARGE8"
    assert row["cutoff"] == "2026-11-24T04:00:00+00:00"
    assert row["metrics"]["cpu"]["avg"] == 25
    assert row["metrics"]["cpu"]["coverage"] == pytest.approx(2 / 3)
    assert row["metrics"]["cpu"]["peak"] == 143.12
    assert row["metrics"]["memory"]["peak"] is None
    assert row["metrics"]["cpu"]["total_used"] == 70
    assert {"repo", "job", "job_name", "scope", "pod_uid"}.isdisjoint(row)
    assert payload["cost"]["available"] is False
    assert client.get("/api/v1/utilization/daily", params={"machine_id": "' OR 1=1 --"}).json()["items"] == []


def test_machine_reads_365_days_with_direct_pagination(client, engine):
    first = NOW.date() - timedelta(days=364)
    insert(engine, usage_date=first.isoformat(), machine_id="old-machine")
    insert(engine, machine_id="new-machine")
    params = {"start_date": first.isoformat(), "end_date": NOW.date().isoformat(), "page_size": 1}
    payload = client.get("/api/v1/utilization/daily", params=params).json()
    assert payload["items"][0]["machine_id"] == "new-machine" and payload["has_more"] is True
    payload = client.get("/api/v1/utilization/daily", params={**params, "page": 2}).json()
    assert payload["items"][0]["machine_id"] == "old-machine" and payload["has_more"] is False
    params["start_date"] = (first - timedelta(days=1)).isoformat()
    assert client.get("/api/v1/utilization/daily", params=params).status_code == 422


@pytest.mark.parametrize("utc_time,today", [
    ("2026-11-23T15:59:59+00:00", date(2026, 11, 23)),
    ("2026-11-23T16:00:00+00:00", date(2026, 11, 24)),
])
def test_machine_single_day_reads_match_advertised_beijing_window(client, engine, monkeypatch, utc_time, today):
    monkeypatch.setattr(queries, "utc_now", lambda: datetime.fromisoformat(utc_time))
    first = today - timedelta(days=364)
    status = client.get("/api/v1/utilization/status").json()
    assert status["machine_window"] == {"start_date": first.isoformat(), "end_date": today.isoformat()}
    for day, accepted in ((first, True), (today, True), (first - timedelta(days=1), False),
                          (today + timedelta(days=1), False), (date(2020, 1, 1), False)):
        insert(engine, usage_date=day.isoformat())
        result = client.get("/api/v1/utilization/daily", params={"start_date": day, "end_date": day})
        assert result.status_code == (200 if accepted else 422)
        if accepted:
            assert [item["usage_date"] for item in result.json()["items"]] == [day.isoformat()]


def test_machine_interval_summary_is_weighted_and_independent_of_page(client, engine):
    insert(engine, usage_date="2026-11-22", cpu=metric(used=60, capacity=120),
           memory=metric(used=0, capacity=0, total_used=9), cpu_peak=80)
    insert(engine, cpu=metric(used=90, capacity=360), memory=metric(used=3, capacity=20),
           cpu_peak=70, memory_peak=27)
    insert(engine, usage_date="2026-11-21", cpu_peak=999, memory_peak=999)
    insert(engine, account_id="other-account", cpu_peak=999, memory_peak=999)
    params = {"machine_id": "ins-example", "start_date": "2026-11-22", "end_date": "2026-11-24",
              "page_size": 1}
    expected = {
        "account_id": queries.ACCOUNT_ID, "region": "ap-beijing", "cluster_id": "",
        "machine_id": "ins-example", "days_with_facts": 2,
        "metrics": {"cpu": {"avg": 31.25, "peak": 80}, "memory": {"avg": 15, "peak": 27}},
    }
    for page, dates, more in ((1, ["2026-11-24"], True), (2, ["2026-11-22"], False), (3, [], False)):
        payload = client.get("/api/v1/utilization/daily", params={**params, "page": page}).json()
        assert payload["interval_summary"] == expected
        assert [row["usage_date"] for row in payload["items"]] == dates
        assert payload["has_more"] is more
    assert client.get("/api/v1/utilization/daily").json().get("interval_summary") is None


def test_duplicate_machine_ids_require_matching_region_and_cluster_for_summary(client, engine):
    insert(engine, cluster_id="cls-a", resource_class="tke_native", memory=metric(used=0, capacity=0))
    insert(engine, usage_date="2026-11-23", cluster_id="cls-b", resource_class="tke_native", cpu_peak=999)
    insert(engine, usage_date="2026-11-22", region="ap-shanghai", cluster_id="cls-a", cpu_peak=999)
    params = {"machine_id": "ins-example", "page_size": 1}
    for scope in ({}, {"region": "ap-beijing"}, {"cluster_id": "cls-a"}):
        payload = client.get("/api/v1/utilization/daily", params={**params, **scope}).json()
        assert payload["interval_summary"] == {"reason": "ambiguous_machine"}
        assert payload["has_more"] is True and len(payload["items"]) == 1
    payload = client.get("/api/v1/utilization/daily", params={
        **params, "region": "ap-beijing", "cluster_id": "cls-a",
    }).json()
    assert payload["has_more"] is False
    summary = payload["interval_summary"]
    assert summary["cluster_id"] == payload["items"][0]["cluster_id"] == "cls-a"
    assert summary["metrics"] == {"cpu": {"avg": 25, "peak": 143.12}, "memory": {"avg": None, "peak": None}}
    payload = client.get("/api/v1/utilization/daily", params={**params, "resource_class": "cvm"}).json()
    assert payload["interval_summary"]["region"] == payload["items"][0]["region"] == "ap-shanghai"


def test_duplicate_machine_ids_on_one_day_keep_database_pagination(client, engine):
    for region in ("ap-beijing", "ap-shanghai"):
        insert(engine, region=region)
    params = {"machine_id": "ins-example", "page_size": 1,
              "start_date": NOW.date(), "end_date": NOW.date()}
    for page, region, has_more in ((1, "ap-beijing", True), (2, "ap-shanghai", False)):
        payload = client.get("/api/v1/utilization/daily", params={**params, "page": page}).json()
        assert payload["interval_summary"] == {"reason": "ambiguous_machine"}
        assert [item["region"] for item in payload["items"]] == [region]
        assert payload["has_more"] is has_more


def test_invalid_off_page_machine_data_withholds_only_the_interval_summary(client, engine):
    insert(engine)
    insert(engine, usage_date="2026-11-23", cpu=metric(used=-1))
    payload = client.get("/api/v1/utilization/daily?machine_id=ins-example&page_size=1").json()
    assert payload["interval_summary"] == {"reason": "invalid_data"}
    assert payload["has_more"] is True and len(payload["items"]) == 1


def test_generic_workload_groups_sum_integrals_not_percentages(client, engine):
    insert(engine, "workload", labels_json=json.dumps({"service": "build", "test_suite": "unit"}),
           cpu=metric(used=10, capacity=20, expected_capacity=100, observed_seconds=30))
    insert(engine, "workload", labels_json=json.dumps({"service": "build", "test_suite": "integration"}),
           cpu=metric(used=9, capacity=180, expected_capacity=300, observed_seconds=60),
           cutoff="2026-11-24 03:00:00")
    insert(engine, "workload", labels_json="{}")
    insert(engine, "workload", labels_json=json.dumps({"service": "expired"}),
           expires_at="2026-11-24 04:00:00")
    insert(engine, "workload", account_id="other-account")
    payload = client.get("/api/v1/utilization/daily?kind=workload&group_by=service").json()
    assert len(payload["items"]) == 2
    assert payload["items"][0]["group_value"] is None  # Unlabeled work is not discarded.
    group = payload["items"][1]
    assert group["group_value"] == "build"
    assert group["metrics"]["cpu"]["avg"] == 9.5  # Not mean(50%, 5%).
    assert group["metrics"]["cpu"]["coverage"] == 0.5  # min(200/400, 90/120).
    assert group["metrics"]["cpu"]["total_used"] == 140
    assert group["cutoff"] == "2026-11-24T03:00:00+00:00"
    assert "peak" not in json.dumps(payload) and "pod_uid" not in json.dumps(payload)
    total = client.get("/api/v1/utilization/daily?kind=workload").json()["items"]
    assert len(total) == 1 and total[0]["metrics"]["cpu"]["used"] == 69
    first = client.get("/api/v1/utilization/daily?kind=workload&group_by=service&page_size=1").json()
    assert first["has_more"] is True
    second = client.get("/api/v1/utilization/daily?kind=workload&group_by=service&page_size=1&page=2").json()
    assert second["items"][0]["group_value"] == "build" and second["has_more"] is False


def test_business_label_values_and_json_paths_are_bound(client, engine):
    value = "' OR 1=1 --"
    insert(engine, "workload", labels_json=json.dumps({"test_project": value}))
    insert(engine, "workload")
    reads = []

    def record(_conn, _cursor, statement, params, *_args):
        reads.append((statement, params))

    event.listen(engine, "before_cursor_execute", record)
    try:
        payload = client.get("/api/v1/utilization/daily", params={
            "kind": "workload", "label_key": "test_project", "label_value": value,
        }).json()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(payload["items"]) == 1
    assert payload["items"][0]["metrics"]["cpu"]["total_used"] == 70
    sql, params = reads[-1]
    assert value not in sql and 'test_project' not in sql
    assert value in params and '$."test_project"' in params


@pytest.mark.parametrize("requests,unknown,zero", [(None, 60, 0), (0, 0, 60)])
def test_unknown_and_known_zero_requests_keep_absolute_usage(client, engine, requests, unknown, zero):
    insert(engine, "workload", cpu_request_cores=requests, cpu=metric(
        used=0, capacity=0, expected_capacity=0, observed_seconds=0,
        total_used=70, unknown_seconds=unknown, zero_seconds=zero,
    ))
    row = client.get("/api/v1/utilization/daily?kind=workload").json()["items"][0]["metrics"]["cpu"]
    assert row["avg"] is None and row["coverage"] == 0
    assert row["total_used"] == 70 and row["usage_seconds"] == 60
    assert row["unknown_seconds"] == unknown and row["zero_seconds"] == zero


def test_missing_metric_is_not_invented_zero(client, engine):
    insert(engine, cpu={field: None for field in FIELDS}, memory=metric(used=0, total_used=0))
    row = client.get("/api/v1/utilization/daily").json()["items"][0]
    assert row["metrics"]["cpu"]["avg"] is None and row["metrics"]["cpu"]["coverage"] is None
    assert row["metrics"]["cpu"]["total_used"] is None
    assert row["metrics"]["memory"]["avg"] == 0 and row["metrics"]["memory"]["total_used"] == 0


def test_workload_null_integrals_cannot_be_treated_as_zero_in_group_averages(client, engine):
    insert(engine, "workload", cpu=metric(used=None, total_used=None))
    insert(engine, "workload")
    row = client.get("/api/v1/utilization/daily?kind=workload").json()["items"][0]
    assert row["metrics"]["cpu"]["used"] is None and row["metrics"]["cpu"]["avg"] is None
    assert row["metrics"]["cpu"]["total_used"] is None
    assert row["metrics"]["memory"]["used"] == 100


def test_mysql_json_filter_expression_is_bound(client, engine, monkeypatch):
    # Exercise the production JSON_UNQUOTE branch offline with an equivalent SQLite function.
    with engine.connect() as connection:
        connection.connection.driver_connection.create_function("JSON_UNQUOTE", 1, lambda value: value)
    monkeypatch.setattr(engine.dialect, "name", "mysql")
    insert(engine, "workload")
    payload = client.get("/api/v1/utilization/daily", params={
        "kind": "workload", "label_key": "service", "label_value": "build",
    }).json()
    assert payload["available"] is True and len(payload["items"]) == 1


def test_workload_scan_fail_closed_without_partial_totals(client, engine, monkeypatch):
    assert queries.WORKLOAD_ROW_LIMIT == 10_000
    monkeypatch.setattr(queries, "WORKLOAD_ROW_LIMIT", 2)
    for _ in range(3):
        insert(engine, "workload")
    payload = client.get("/api/v1/utilization/daily?kind=workload").json()
    assert payload["reason"] == "query_too_large" and payload["items"] == []
    assert payload["row_limit"] == 2


@pytest.mark.parametrize("bad_labels", ["invalid-json", "[]", '{"service":123}'])
def test_invalid_stored_labels_fail_closed(client, engine, bad_labels):
    insert(engine, "workload", labels_json=bad_labels)
    payload = client.get("/api/v1/utilization/daily?kind=workload&group_by=service").json()
    assert payload["reason"] == "invalid_data" and payload["items"] == []


@pytest.mark.parametrize("kind", ["machine", "workload"])
def test_invalid_stored_integrals_fail_closed(client, engine, kind):
    insert(engine, kind, cpu=metric(used=-1))
    assert client.get(f"/api/v1/utilization/daily?kind={kind}").json()["reason"] == "invalid_data"


def test_nonfinite_peaks_or_overflowed_ratios_fail_closed(client, engine):
    insert(engine, cpu_peak=float("inf"))
    assert client.get("/api/v1/utilization/daily").json()["reason"] == "invalid_data"
    insert(engine, "workload", cpu=metric(used=1e308, capacity=1e-308))
    assert client.get("/api/v1/utilization/daily?kind=workload").json()["reason"] == "invalid_data"


def test_status_uses_v2_guard_active_contexts_backlog_and_cutoffs(client, engine):
    # Today's successful planner has collected the full D-3 Beijing day.
    insert(engine, usage_date="2026-11-21", cutoff="2026-11-21 16:00:00")
    insert(engine, "workload")
    with engine.begin() as connection:
        for closed, expires, account in ((None, "2026-11-25", queries.ACCOUNT_ID),
                                         ("2026-11-24", "2026-11-25", queries.ACCOUNT_ID),
                                         (None, "2026-11-23", queries.ACCOUNT_ID),
                                         (None, "2026-11-25", "other")):
            connection.execute(text("INSERT INTO pod_utilization_context "
                                    "(account_id,closed_at,expires_at) VALUES (:a,:c,:e)"),
                               {"a": account, "c": closed, "e": expires})
        for index, status in enumerate(("pending", "retry", "running", "done", "expired")):
            connection.execute(text("INSERT INTO utilization_task (id,account_id,status,deadline_at) "
                                    "VALUES (:id,:a,:s,'2026-11-25')"),
                               {"id": str(index), "a": queries.ACCOUNT_ID, "s": status})
        connection.execute(text("INSERT INTO utilization_task (id,account_id,status,deadline_at) "
                                "VALUES ('past',:a,'retry','2026-11-23')"), {"a": queries.ACCOUNT_ID})
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["configured"] is True and payload["healthy"] is True
    assert payload["active_contexts"] == 1
    assert payload["task_backlog"] == {"pending": 1, "running": 1, "retry": 1}
    assert payload["latest_cutoff"] == "2026-11-21T16:00:00+00:00"
    assert payload["stale"] is False and payload["maintenance_stale"] is False
    assert payload["label_keys"] == ["repo", "job", "test_project", "test_suite", "environment", "project", "service"]
    assert payload["machine_window"] == {"start_date": "2025-11-25", "end_date": "2026-11-24"}
    assert payload["workload_window"]["start_date"] == "2026-11-16"
    assert "contracts" not in payload and "scope" not in payload and "payload_json" not in payload
    configure(engine, paused=True, pause_reasons=["budget"])
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["reason"] == "unhealthy" and payload["paused"] is True
    assert payload["pause_reasons"] == ["budget"]


@pytest.mark.parametrize("clock,cutoff,stale", [
    ("2026-11-23T22:29:59+00:00", "2026-11-20 16:00:00", False),
    ("2026-11-23T23:29:59+00:00", "2026-11-20 16:00:00", False),
    ("2026-11-23T23:30:00+00:00", "2026-11-20 16:00:00", True),
    ("2026-11-24T05:00:00+00:00", "2026-11-21 16:00:00", False),
    ("2026-11-24T21:00:00+00:00", "2026-11-21 16:00:00", False),
    ("2026-11-24T05:00:00+00:00", "2026-11-21 15:59:59", True),
])
def test_status_staleness_follows_daily_historical_planner(client, engine, monkeypatch, clock, cutoff, stale):
    clock = datetime.fromisoformat(clock)
    monkeypatch.setattr(queries, "utc_now", lambda: clock)
    configure(engine, maintenance_at=clock.isoformat(), bytes_observed_at=clock.isoformat())
    insert(engine, usage_date=cutoff[:10], cutoff=cutoff)
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["healthy"] is True
    assert payload["stale"] is stale


@pytest.mark.parametrize("extra", [
    {"version": 1}, {"contracts": {}}, {"scope": {}}, {"scope": {"account_id": "other"}},
])
def test_v1_or_incomplete_guard_cannot_configure_v2(client, engine, extra):
    configure(engine, **extra)
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["configured"] is False and payload["reason"] == "unconfigured"
    assert client.get("/api/v1/utilization/daily").json()["reason"] == "unconfigured"


@pytest.mark.parametrize("maintenance", [None, "bad", "2026-11-23T00:00:00Z"])
def test_status_missing_or_stale_maintenance_is_unhealthy(client, engine, maintenance):
    configure(engine, maintenance_at=maintenance)
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["reason"] == "unhealthy" and payload["maintenance_stale"] is True
    assert payload["latest_cutoff"] is None and payload["stale"] is True


@pytest.mark.parametrize("path,params", [
    ("daily", {"page": 1001}), ("daily", {"page_size": 0}), ("daily", {"kind": "person"}),
    ("daily", {"scope": "job"}), ("daily", {"repo": "org/repo"}),
    ("daily", {"job_name": "stable"}), ("daily", {"owner": "someone"}),
    ("daily", {"start_date": "2026-11-16"}),
    ("daily", {"start_date": "2026-11-24", "end_date": "2026-11-25"}),
    ("daily", {"start_date": "2026-11-24", "end_date": "2026-11-23"}),
    ("daily", {"start_date": "0001-01-01", "end_date": "2026-11-23"}),
    ("daily", {"start_date": "2020-01-01", "end_date": "2020-01-01"}),
    ("daily", {"kind": "workload", "start_date": "2026-11-15", "end_date": "2026-11-24"}),
    ("daily", {"kind": "workload", "machine_id": "node"}),
    ("daily", {"kind": "workload", "cluster_id": "cls-a"}),
    ("daily", {"cluster_id": "x" * 513}),
    ("daily", {"kind": "machine", "group_by": "service"}),
    ("daily", {"kind": "workload", "group_by": "author"}),
    ("daily", {"kind": "workload", "label_key": 'repo\") OR 1=1 --', "label_value": "x"}),
    ("daily", {"kind": "workload", "label_key": "service"}),
    ("daily", {"kind": "workload", "label_value": "build"}),
    ("daily", {"resource_class": "pod"}), ("daily", {"machine_id": "x" * 513}),
    ("daily", {"start_date": "not-a-date"}), ("weekly", {"metric": "cpu"}),
    ("status", {"owner": "someone"}),
])
def test_invalid_inputs_do_not_query_database(client, engine, path, params):
    reads = []

    def record(_conn, _cursor, statement, *_args):
        reads.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert client.get(f"/api/v1/utilization/{path}", params=params).status_code == 422
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert reads == []


@pytest.mark.parametrize("extra,reason", [
    ({"counts_reliable": False}, "counts_unreliable"),
    ({"retained_rows": 2_000_000}, "row_budget"),
    ({"observed_bytes": None}, "bytes_unreliable"),
    ({"observed_bytes": 8 * 1024**3}, "byte_budget"),
    ({"enabled": False}, "collection_disabled"),
    ({"bytes_observed_at": None}, "bytes_observed_at_stale"),
    ({"maintenance_at": "2026-11-24T06:00:00Z"}, "maintenance_at_stale"),
])
def test_status_guard_budget_and_clock_health(client, engine, extra, reason):
    configure(engine, **extra)
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["configured"] is True and payload["healthy"] is False
    assert payload["reason"] == "unhealthy" and reason in payload["health_reasons"]


def test_invalid_guard_json_is_unconfigured(client, engine):
    with engine.begin() as connection:
        connection.execute(text("UPDATE cost_job_state SET watermark_json='bad-json'"))
    assert client.get("/api/v1/utilization/status").json()["reason"] == "unconfigured"


def test_labels_must_also_be_enabled_in_guard_scope(client, engine):
    configure(engine, scope={"account_id": queries.ACCOUNT_ID, "labels": ["service"]})
    assert client.get("/api/v1/utilization/daily?kind=workload&group_by=repo").status_code == 422


def test_beijing_week_boundary():
    assert queries.date_range(None, None, kind="workload", now=datetime(2026, 11, 22, 16, tzinfo=UTC)) == (
        date(2026, 11, 16), date(2026, 11, 23),
    )


@pytest.mark.parametrize("table", [
    "machine_utilization_daily", "workload_utilization_daily", "pod_utilization_context",
    "utilization_task", "cost_job_state",
])
def test_missing_v2_schema_is_informative(client, engine, table):
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE {table}"))
    assert client.get("/api/v1/utilization/status").json()["reason"] == "missing_schema"
    assert client.get("/api/v1/utilization/weekly").json()["reason"] == "not_supported"
    if table in {"machine_utilization_daily", "workload_utilization_daily", "cost_job_state"}:
        kind = "workload" if table == "workload_utilization_daily" else "machine"
        assert client.get(f"/api/v1/utilization/daily?kind={kind}").json()["reason"] == "missing_schema"
    assert client.get("/healthz").status_code == 200


def test_database_construction_failure_is_informative(monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")

    def fail():
        raise ValueError("missing DB config")

    monkeypatch.setattr(routes, "get_engine", fail)
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/utilization/status").json()["reason"] == "database_unavailable"
    assert queries.get_daily(None)["reason"] == "database_unavailable"
    assert queries.get_status(None)["reason"] == "database_unavailable"


def test_database_read_failure_is_not_reported_as_missing_schema(client, engine):
    def fail(*_args):
        from sqlalchemy.exc import OperationalError
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        assert client.get("/api/v1/utilization/status").json()["reason"] == "database_unavailable"
        assert client.get("/api/v1/utilization/daily").json()["reason"] == "database_unavailable"
    finally:
        event.remove(engine, "before_cursor_execute", fail)


@pytest.mark.parametrize("params", [
    {"kind": "bogus"}, {"page": 1001}, {"machine_id": "x" * 513},
    {"resource_class": "pod"}, {"kind": "workload", "label_key": "author", "label_value": "x"},
])
def test_direct_query_validation(engine, monkeypatch, params):
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")
    with pytest.raises(ValueError):
        queries.get_daily(engine, **params)


def test_utilization_keeps_existing_company_auth_gate(monkeypatch):
    from ci_dashboard.api.auth import get_identity_verifier

    monkeypatch.delenv("CI_DASHBOARD_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("CI_DASHBOARD_OIDC_CLIENT_ID", raising=False)
    get_identity_verifier.cache_clear()
    with TestClient(create_app()) as client:
        for path in ("status", "daily", "weekly"):
            assert client.get(f"/api/v1/utilization/{path}").status_code == 503
