from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from ci_dashboard.api.main import create_app
from ci_dashboard.api.queries import utilization as queries
from ci_dashboard.api.routes import utilization as routes

NOW = datetime(2026, 11, 24, 5, tzinfo=UTC)
MISSING_MACHINE_METRIC = {"percent_sum": 0, "sample_minutes": 0, "peak_percent": None}


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE machine_utilization_daily (
            id INTEGER PRIMARY KEY, account_id TEXT, region TEXT, cluster_id TEXT,
            machine_id TEXT, usage_date DATE, resource_class TEXT,
            cpu_percent_sum REAL, cpu_sample_minutes INTEGER, cpu_peak_percent REAL,
            memory_percent_sum REAL, memory_sample_minutes INTEGER, memory_peak_percent REAL,
            UNIQUE (account_id, region, cluster_id, machine_id, usage_date))"""))
        connection.execute(text("""CREATE TABLE workload_utilization_daily (
            account_id TEXT, usage_date DATE, group_key TEXT, labels_json JSON,
            cpu_used REAL, cpu_requested REAL, cpu_matched_pod_minutes INTEGER,
            memory_used REAL, memory_requested REAL, memory_matched_pod_minutes INTEGER,
            expected_pod_minutes INTEGER, PRIMARY KEY (account_id, usage_date, group_key))"""))
        connection.execute(text("""CREATE TABLE pod_utilization_context (
            id INTEGER PRIMARY KEY, account_id TEXT, closed_at DATETIME, expires_at DATETIME,
            planned_until DATETIME, observed_until DATETIME)"""))
        connection.execute(text("""CREATE TABLE utilization_task (
            id TEXT PRIMARY KEY, account_id TEXT, status TEXT, deadline_at DATETIME,
            kind TEXT, window_start DATETIME, window_end DATETIME)"""))
        connection.execute(text(
            "CREATE TABLE cost_job_state (job_name TEXT PRIMARY KEY, watermark_json JSON)"
        ))
    configure(engine)

    def no_legacy_reads(_conn, _cursor, statement, *_args):
        assert "cost_utilization_" not in statement, "V3 must never access legacy fact tables"

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
        "version": 3, "enabled": True, "pod_collection_enabled": True,
        "contracts": {"cvm": {"cpu": {"contract": {"numerator": "percent"}}}},
        "scope": {"account_id": queries.ACCOUNT_ID,
                  "labels": ["repo", "job", "test_project", "test_plan", "environment", "project", "service"]},
        "maintenance_at": "2026-11-24T04:00:00", "paused": False,
        "pause_reasons": [], "counts_reliable": True, "retained_rows": 0,
        "observed_bytes": 0, "bytes_observed_at": "2026-11-24T04:00:00", **extra,
    }
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM cost_job_state"))
        connection.execute(text("INSERT INTO cost_job_state VALUES (:key,:state)"),
                           {"key": queries.STATE_KEY, "state": json.dumps(state)})


def machine_metric(total=250, count=10, peak=93.12):
    return {"percent_sum": total, "sample_minutes": count, "peak_percent": peak}


def workload_metric(used=50, requested=200, matched=4):
    return {"used": used, "requested": requested, "matched_pod_minutes": matched}


def insert(engine, kind="machine", cpu=None, memory=None, **extra):
    row = {"account_id": queries.ACCOUNT_ID, "usage_date": "2026-11-24"}
    if kind == "machine":
        row.update(region="ap-beijing", cluster_id="", machine_id="ins-example", resource_class="cvm")
        defaults = (machine_metric(), MISSING_MACHINE_METRIC)
    else:
        row.update(labels_json=json.dumps({"service": "build"}), expected_pod_minutes=6)
        defaults = (workload_metric(), workload_metric(matched=3))
    for prefix, values, default in zip(("cpu", "memory"), (cpu, memory), defaults, strict=True):
        row.update({f"{prefix}_{key}": value for key, value in (values or default).items()})
    row.update(extra)
    if kind == "workload":
        row.setdefault("group_key", sha256(row["labels_json"].encode()).hexdigest())
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


def add_unsettled(engine, kind, start, end, *, account=queries.ACCOUNT_ID):
    with engine.begin() as connection:
        if kind == "task":
            connection.execute(text(
                "INSERT INTO utilization_task (id,account_id,status,kind,window_start,window_end,deadline_at) "
                "VALUES (:start,:account,'pending','pod',:start,:end,:deadline)"
            ), {"account": account, "start": start, "end": end,
                "deadline": (NOW + timedelta(days=1)).replace(tzinfo=None)})
        else:
            connection.execute(text(
                "INSERT INTO pod_utilization_context (account_id,planned_until,observed_until) "
                "VALUES (:account,:start,:end)"
            ), {"account": account, "start": start, "end": end})


@pytest.mark.parametrize("unsettled", ["task", "tail"])
def test_workload_coverage_is_partial_until_confirmed_windows_settle(client, engine, unsettled):
    full_window = workload_metric(matched=6)
    insert(engine, "workload", cpu=full_window, memory=full_window)
    params = {"kind": "workload", "start_date": "2026-11-24", "end_date": "2026-11-24"}
    assert client.get("/api/v1/utilization/daily", params=params).json()["partial"] is False
    add_unsettled(engine, unsettled, "2026-11-23 16:00:00.000000", "2026-11-23 16:10:00.000000")
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


def test_machine_identity_observed_peak_and_derived_metrics(client, engine):
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
    assert row["metrics"] == {
        "cpu": {"avg": 25, "peak": 93.12, "sample_minutes": 10},
        "memory": {"avg": None, "peak": None, "sample_minutes": 0},
    }
    assert set(row) == {"account_id", "usage_date", "region", "cluster_id", "machine_id", "resource_class", "metrics"}
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
    insert(engine, usage_date="2026-11-22", cpu=machine_metric(80, 2, 75))
    insert(engine, cpu=machine_metric(120, 6, 70), memory=machine_metric(0, 1, 0))
    insert(engine, usage_date="2026-11-21", cpu_peak_percent=100)
    insert(engine, account_id="other-account", cpu_peak_percent=100)
    params = {"machine_id": "ins-example", "start_date": "2026-11-22", "end_date": "2026-11-24",
              "page_size": 1}
    expected = {
        "account_id": queries.ACCOUNT_ID, "region": "ap-beijing", "cluster_id": "",
        "machine_id": "ins-example", "days_with_facts": 2,
        "metrics": {"cpu": {"avg": 25, "peak": 75, "sample_minutes": 8},
                    "memory": {"avg": 0, "peak": 0, "sample_minutes": 1}},
    }
    for page, dates, more in ((1, ["2026-11-24"], True), (2, ["2026-11-22"], False), (3, [], False)):
        payload = client.get("/api/v1/utilization/daily", params={**params, "page": page}).json()
        assert payload["interval_summary"] == expected
        assert [row["usage_date"] for row in payload["items"]] == dates
        assert payload["has_more"] is more
    assert client.get("/api/v1/utilization/daily").json().get("interval_summary") is None


def test_duplicate_machine_ids_require_matching_region_and_cluster_for_summary(client, engine):
    insert(engine, cluster_id="cls-a", resource_class="tke_native")
    insert(engine, usage_date="2026-11-23", cluster_id="cls-b", resource_class="tke_native", cpu_peak_percent=100)
    insert(engine, usage_date="2026-11-22", region="ap-shanghai", cluster_id="cls-a", cpu_peak_percent=100)
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
    assert summary["metrics"] == payload["items"][0]["metrics"]
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
    insert(engine, usage_date="2026-11-23", cpu=machine_metric(total=-1))
    payload = client.get("/api/v1/utilization/daily?machine_id=ins-example&page_size=1").json()
    assert payload["interval_summary"] == {"reason": "invalid_data"}
    assert payload["has_more"] is True and len(payload["items"]) == 1


def test_generic_workload_groups_sum_integrals_not_percentages(client, engine):
    labels = json.dumps({"repo": "pingcap/tidb", "job": "build"})
    insert(engine, "workload", labels_json=labels, expected_pod_minutes=4,
           cpu=workload_metric(10, 20, 2), memory=workload_metric(0, 0, 0))
    insert(engine, "workload", labels_json=labels, usage_date="2026-11-16", expected_pod_minutes=8,
           cpu=workload_metric(9, 180, 7), memory=workload_metric(20, 40, 4))
    insert(engine, "workload", labels_json=json.dumps({"repo": "pingcap/tidb"}))
    insert(engine, "workload", labels_json=json.dumps({"repo": "pingcap/tidb", "job": "expired"}),
           usage_date="2026-11-15")
    insert(engine, "workload", labels_json=labels, account_id="other-account")
    params = {"kind": "workload", "group_by": "job", "label_key": "repo", "label_value": "pingcap/tidb"}
    payload = client.get("/api/v1/utilization/daily", params=params).json()
    assert len(payload["items"]) == 2
    assert payload["items"][0]["group_value"] is None  # Unlabeled work is not discarded.
    assert payload["items"][1] == {
        "group_value": "build", "metrics": {
            "cpu": {"avg": 9.5, "coverage": 0.75},  # Not mean(50%, 5%).
            "memory": {"avg": 50, "coverage": 4 / 12},
        },
    }
    total = client.get("/api/v1/utilization/daily?kind=workload").json()["items"]
    assert len(total) == 1 and total[0]["metrics"]["cpu"]["avg"] == pytest.approx(100 * 69 / 400)
    first = client.get("/api/v1/utilization/daily", params={**params, "page_size": 1}).json()
    assert first["has_more"] is True
    second = client.get("/api/v1/utilization/daily", params={**params, "page_size": 1, "page": 2}).json()
    assert second["items"][0]["group_value"] == "build" and second["has_more"] is False


def test_business_label_values_and_json_paths_are_bound(client, engine):
    value = "' OR 1=1 --"
    insert(engine, "workload", labels_json=json.dumps({"test_project": value, "test_plan": "nightly"}))
    insert(engine, "workload")
    reads = []

    def record(_conn, _cursor, statement, params, *_args):
        reads.append((statement, params))

    event.listen(engine, "before_cursor_execute", record)
    try:
        payload = client.get("/api/v1/utilization/daily", params={
            "kind": "workload", "label_key": "test_project", "label_value": value, "group_by": "test_plan",
        }).json()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(payload["items"]) == 1
    assert payload["items"][0]["group_value"] == "nightly"
    assert payload["items"][0]["metrics"]["cpu"] == {"avg": 25, "coverage": 4 / 6}
    sql, params = reads[-1]
    assert value not in sql and 'test_project' not in sql
    assert value in params and '$."test_project"' in params


def test_workload_unmatched_minutes_and_measured_zero_are_distinct(client, engine):
    # Unknown or zero requests produce no matched integrals, but still contribute expected minutes.
    insert(engine, "workload", cpu=workload_metric(0, 0, 0), memory=workload_metric(0, 200, 4))
    metrics = client.get("/api/v1/utilization/daily?kind=workload").json()["items"][0]["metrics"]
    assert metrics == {"cpu": {"avg": None, "coverage": 0}, "memory": {"avg": 0, "coverage": 4 / 6}}


def test_missing_machine_metric_is_not_invented_zero(client, engine):
    assert client.get("/api/v1/utilization/daily").json()["items"] == []
    insert(engine, cpu=MISSING_MACHINE_METRIC, memory=machine_metric(0, 1, 0))
    row = client.get("/api/v1/utilization/daily").json()["items"][0]
    assert row["metrics"] == {
        "cpu": {"avg": None, "peak": None, "sample_minutes": 0},
        "memory": {"avg": 0, "peak": 0, "sample_minutes": 1},
    }


def test_null_workload_integrals_fail_closed(client, engine):
    insert(engine, "workload", cpu=workload_metric(used=None), usage_date="2026-11-23")
    insert(engine, "workload")
    payload = client.get("/api/v1/utilization/daily?kind=workload").json()
    assert payload["reason"] == "invalid_data" and payload["items"] == []


def test_mysql_json_filter_expression_is_bound(client, engine, monkeypatch):
    # Exercise the production JSON_UNQUOTE branch offline with an equivalent SQLite function.
    with engine.connect() as connection:
        connection.connection.driver_connection.create_function("JSON_UNQUOTE", 1, lambda value: value)
    monkeypatch.setattr(engine.dialect, "name", "mysql")
    # Do not send MySQL's date arithmetic to SQLite in this JSON-specific test.
    monkeypatch.setattr(queries, "_workload_partial", lambda *_args: False)
    insert(engine, "workload")
    payload = client.get("/api/v1/utilization/daily", params={
        "kind": "workload", "label_key": "service", "label_value": "build",
    }).json()
    assert payload["available"] is True and len(payload["items"]) == 1


def test_workload_scan_fail_closed_without_partial_totals(client, engine, monkeypatch):
    assert queries.WORKLOAD_ROW_LIMIT == 10_000
    monkeypatch.setattr(queries, "WORKLOAD_ROW_LIMIT", 2)
    for job in ("compile", "unit", "integration"):
        insert(engine, "workload", labels_json=json.dumps({"job": job}))
    payload = client.get("/api/v1/utilization/daily?kind=workload").json()
    assert payload["reason"] == "query_too_large" and payload["items"] == []
    assert payload["row_limit"] == 2


@pytest.mark.parametrize("bad_labels", ["invalid-json", "[]", '{"service":123}'])
def test_invalid_stored_labels_fail_closed(client, engine, bad_labels):
    insert(engine, "workload", labels_json=bad_labels)
    payload = client.get("/api/v1/utilization/daily?kind=workload&group_by=service").json()
    assert payload["reason"] == "invalid_data" and payload["items"] == []


def test_negative_or_overmatched_workload_facts_fail_closed(client, engine):
    insert(engine, "workload", cpu=workload_metric(used=-1))
    assert client.get("/api/v1/utilization/daily?kind=workload").json()["reason"] == "invalid_data"
    with engine.begin() as connection:
        connection.execute(text("UPDATE workload_utilization_daily SET cpu_used=0, cpu_matched_pod_minutes=7"))
    assert client.get("/api/v1/utilization/daily?kind=workload").json()["reason"] == "invalid_data"


def test_nonfinite_peaks_or_overflowed_ratios_fail_closed(client, engine):
    insert(engine, cpu_peak_percent=float("inf"))
    assert client.get("/api/v1/utilization/daily").json()["reason"] == "invalid_data"
    insert(engine, "workload", cpu=workload_metric(used=1e308, requested=1e-308))
    assert client.get("/api/v1/utilization/daily?kind=workload").json()["reason"] == "invalid_data"


def test_status_uses_v3_guard_active_contexts_backlog_and_latest_machine_usage_day(client, engine):
    insert(engine, usage_date="2026-11-21")
    insert(engine, usage_date="2026-11-20")
    insert(engine, account_id="other-account")
    insert(engine, usage_date="2026-11-25")
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
    assert payload["version"] == 3 and payload["configured"] is True and payload["healthy"] is True
    assert payload["active_contexts"] == 1
    assert payload["task_backlog"] == {"pending": 1, "running": 1, "retry": 1}
    assert payload["latest_machine_usage_date"] == "2026-11-21"
    assert payload["pod_collection_enabled"] is True and payload["workload_history_available"] is True
    assert payload["maintenance_stale"] is False
    assert payload["label_keys"] == ["repo", "job", "test_project", "test_plan", "environment", "project", "service"]
    assert {"latest_cutoff", "workload_cutoff", "stale"}.isdisjoint(payload)
    assert payload["machine_window"] == {"start_date": "2025-11-25", "end_date": "2026-11-24"}
    assert payload["workload_window"]["start_date"] == "2026-11-16"
    assert "contracts" not in payload and "scope" not in payload and "payload_json" not in payload
    configure(engine, paused=True, pause_reasons=["budget"])
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["reason"] == "unhealthy" and payload["paused"] is True
    assert payload["pause_reasons"] == ["budget"]


def test_machine_only_source_works_without_pod_tables_or_status(client, engine):
    configure(engine, pod_collection_enabled=False)
    insert(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE workload_utilization_daily"))
        connection.execute(text("DROP TABLE pod_utilization_context"))
        connection.execute(text("INSERT INTO utilization_task (id,account_id,status,deadline_at,kind) "
                                "VALUES ('pod',:a,'retry','2026-11-25','pod'),"
                                "('machine',:a,'pending','2026-11-25','machine')"), {"a": queries.ACCOUNT_ID})
    status = client.get("/api/v1/utilization/status").json()
    assert status["healthy"] is True and status["latest_machine_usage_date"] == "2026-11-24"
    assert status["pod_collection_enabled"] is False and status["workload_history_available"] is False
    assert status["pod_collection_stopped"] is False and status["workload_draining"] is False
    assert {"workload_window", "active_contexts", "label_keys"}.isdisjoint(status)
    assert status["task_backlog"] == {"pending": 1, "running": 0, "retry": 0}
    assert client.get("/api/v1/utilization/daily").json()["available"] is True
    response = client.get("/api/v1/utilization/daily?kind=workload").json()
    assert response["reason"] == "pod_collection_disabled" and "partial" not in response


@pytest.mark.parametrize("unsettled", ["task", "tail"])
def test_disabled_pod_collection_can_read_retained_history_and_caps_partial_at_stop(client, engine, unsettled):
    configure(engine, pod_collection_enabled=False, pod_stop_at="2026-11-24T04:10:30Z")
    insert(engine, "workload")
    add_unsettled(engine, unsettled, "2026-11-24 04:10:00.000000", "2026-11-24 04:30:00.000000")
    status = client.get("/api/v1/utilization/status").json()
    assert status["pod_collection_enabled"] is False and status["workload_history_available"] is True
    assert "active_contexts" not in status and "workload_window" in status
    path = "/api/v1/utilization/daily?kind=workload&label_key=service&label_value=build"
    payload = client.get(path).json()
    assert payload["items"][0]["metrics"]["cpu"] == {"avg": 25, "coverage": 4 / 6}
    assert payload["partial"] is False
    add_unsettled(engine, unsettled, "2026-11-24 04:09:00.000000", "2026-11-24 04:10:00.000000")
    assert client.get(path).json()["partial"] is True
    # The settled fact is physically present, but has aged out of the serving window.
    with engine.begin() as connection:
        connection.execute(text("UPDATE workload_utilization_daily SET usage_date='2026-11-15'"))
        if unsettled == "task":
            connection.execute(text("UPDATE utilization_task SET status='done' WHERE window_end<='2026-11-24 04:10:30'"))
        else:
            connection.execute(text("UPDATE pod_utilization_context SET planned_until=observed_until"))
    status = client.get("/api/v1/utilization/status").json()
    assert status["workload_history_available"] is False and status["workload_draining"] is False
    assert status["pod_collection_stopped"] is True
    assert client.get(path).json()["reason"] == "pod_collection_disabled"


@pytest.mark.parametrize("unsettled,end", [("task", "04:10:00"), ("tail", "04:30:00")])
def test_stopped_workload_drains_before_the_first_fact(client, engine, unsettled, end):
    configure(engine, pod_collection_enabled=False, pod_stop_at="2026-11-24T04:10:30Z")
    add_unsettled(engine, unsettled, "2026-11-24 04:00:00.000000", f"2026-11-24 {end}.000000")
    path = "/api/v1/utilization/daily?kind=workload&label_key=service&label_value=build"
    result = client.get(path).json()
    assert result["available"] is True and result["partial"] is True and result["items"] == []
    status = client.get("/api/v1/utilization/status").json()
    assert status["pod_collection_enabled"] is False and status["pod_collection_stopped"] is True
    assert status["workload_draining"] is True and status["workload_history_available"] is False
    assert status["task_backlog"]["pending"] == (1 if unsettled == "task" else 0)
    assert status["workload_window"] == status["default_window"] and "service" in status["label_keys"]
    other_day = client.get(path + "&start_date=2026-11-23&end_date=2026-11-23").json()
    assert other_day["available"] is True and other_day["partial"] is False and other_day["items"] == []
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM utilization_task"))
        connection.execute(text("DELETE FROM pod_utilization_context"))
    insert(engine, "workload")
    status = client.get("/api/v1/utilization/status").json()
    assert status["workload_history_available"] is True and status["workload_draining"] is False
    result = client.get(path).json()
    assert result["available"] is True and result["partial"] is False and len(result["items"]) == 1


def test_stopped_workload_ignores_out_of_scope_or_unconfirmed_work(client, engine):
    configure(engine, pod_collection_enabled=False, pod_stop_at="2026-11-24T04:10:30Z")
    for unsettled in ("task", "tail"):
        # Nov 15 Beijing is out of retention; absence of whole minutes around the cutoff is not work.
        add_unsettled(engine, unsettled, "2026-11-15 15:50:00.000000", "2026-11-15 16:00:00.000000")
        add_unsettled(engine, unsettled, "2026-11-24 04:10:00.000000", "2026-11-24 04:30:00.000000")
        add_unsettled(engine, unsettled, "2026-11-24 04:00:00.000000", "2026-11-24 04:10:00.000000",
                      account="other")
    add_unsettled(engine, "task", "2026-11-24 04:09:00.000000", "2026-11-24 04:11:00.000000")
    add_unsettled(engine, "tail", "2026-11-24 04:09:01.000000", "2026-11-24 04:11:00.000000")
    status = client.get("/api/v1/utilization/status").json()
    assert status["pod_collection_stopped"] is True and status["workload_draining"] is False
    assert "workload_window" not in status
    assert client.get("/api/v1/utilization/daily?kind=workload").json()["reason"] == "pod_collection_disabled"


@pytest.mark.parametrize("cutoff", [None, "not a date", "2026-11-24T04:10:30", "2026-11-24T12:10:30+08:00"])
def test_pod_drain_requires_a_trusted_cutoff(client, engine, cutoff):
    configure(engine, pod_collection_enabled=False, pod_stop_at=cutoff)
    add_unsettled(engine, "task", "2026-11-24 04:00:00.000000", "2026-11-24 04:10:00.000000")
    status = client.get("/api/v1/utilization/status").json()
    assert status["pod_collection_stopped"] is False and status["workload_draining"] is False
    assert client.get("/api/v1/utilization/daily?kind=workload").json()["reason"] == "pod_collection_disabled"


def test_workload_partial_requires_a_whole_minute_and_not_future_or_other_account(client, engine):
    configure(engine, pod_stop_at="2026-11-22T04:10:30Z")  # An old stop doesn't cap a reopened source.
    path = "/api/v1/utilization/daily?kind=workload&start_date=2026-11-24&end_date=2026-11-24"
    status = client.get("/api/v1/utilization/status").json()
    assert status["pod_collection_enabled"] is True and status["workload_history_available"] is False
    assert status["pod_collection_stopped"] is False and status["workload_draining"] is False
    add_unsettled(engine, "tail", "2026-11-24 04:00:01.000000", "2026-11-24 04:01:59.999999")
    add_unsettled(engine, "tail", "2026-11-24 05:00:00.000000", "2026-11-24 05:10:00.000000")
    add_unsettled(engine, "task", "2026-11-24 04:00:00.000000", "2026-11-24 04:01:00.000000", account="other")
    add_unsettled(engine, "task", "2026-11-23 15:59:00.000000", "2026-11-23 16:00:00.000000")
    payload = client.get(path).json()
    assert payload["available"] is True and payload["items"] == [] and payload["partial"] is False
    add_unsettled(engine, "task", "2026-11-23 15:58:00.000000", "2026-11-23 16:01:00.000000")
    assert client.get(path).json()["partial"] is True


@pytest.mark.parametrize("extra", [
    {"version": 1}, {"version": 2}, {"contracts": {}}, {"scope": {}}, {"scope": {"account_id": "other"}},
])
def test_legacy_or_incomplete_guard_cannot_configure_v3(client, engine, extra):
    configure(engine, **extra)
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["configured"] is False and payload["reason"] == "unconfigured"
    assert client.get("/api/v1/utilization/daily").json()["reason"] == "unconfigured"


@pytest.mark.parametrize("maintenance", [None, "bad", "2026-11-23T00:00:00Z"])
def test_status_missing_or_stale_maintenance_is_unhealthy(client, engine, maintenance):
    configure(engine, maintenance_at=maintenance)
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["reason"] == "unhealthy" and payload["maintenance_stale"] is True
    assert payload["latest_machine_usage_date"] is None


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


def test_identity_labels_cannot_be_advertised_filtered_or_grouped(client, engine):
    canonical = ["pod_uid", "pod_name", "build_id", "build_number", "run_id", "execution_id",
                 "test_execution_id", "job_uid", "job_run_id"]
    denied = [*canonical, *(key.upper().replace("_", ".", 1).replace("_", "-") for key in canonical),
              "Author", "Employee-ID", "podUID", "testExecutionId"]
    allowed = ["repo", "job", "test_project", "test_plan", "job_name", "service.name"]
    configure(engine, scope={"account_id": queries.ACCOUNT_ID, "labels": [*denied, *allowed]})
    status = client.get("/api/v1/utilization/status").json()
    assert status["label_keys"] == allowed
    for key in denied:
        for filters in ({"group_by": key}, {"label_key": key, "label_value": "one"}):
            result = client.get("/api/v1/utilization/daily", params={"kind": "workload", **filters})
            assert result.status_code == 422, (key, filters)
    result = client.get("/api/v1/utilization/daily", params={
        "kind": "workload", "group_by": "service.name", "label_key": "job_name", "label_value": "compile",
    })
    assert result.status_code == 200 and result.json()["available"] is True


def test_beijing_week_boundary():
    assert queries.date_range(None, None, kind="workload", now=datetime(2026, 11, 22, 16, tzinfo=UTC)) == (
        date(2026, 11, 16), date(2026, 11, 23),
    )


@pytest.mark.parametrize("table", [
    "machine_utilization_daily", "workload_utilization_daily", "pod_utilization_context",
    "utilization_task", "cost_job_state",
])
def test_missing_v3_schema_is_informative(client, engine, table):
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE {table}"))
    assert client.get("/api/v1/utilization/status").json()["reason"] == "missing_schema"
    assert client.get("/api/v1/utilization/weekly").json()["reason"] == "not_supported"
    if table in {"machine_utilization_daily", "workload_utilization_daily", "cost_job_state"}:
        kind = "workload" if table == "workload_utilization_daily" else "machine"
        assert client.get(f"/api/v1/utilization/daily?kind={kind}").json()["reason"] == "missing_schema"
    assert client.get("/healthz").status_code == 200


def test_status_reports_missing_schema_before_first_configuration(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM cost_job_state"))
        connection.execute(text("DROP TABLE machine_utilization_daily"))
    assert client.get("/api/v1/utilization/status").json()["reason"] == "missing_schema"


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
