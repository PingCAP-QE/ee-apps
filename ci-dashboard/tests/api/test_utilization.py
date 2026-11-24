from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from ci_dashboard.api.main import create_app
from ci_dashboard.api.queries import utilization as queries
from ci_dashboard.api.routes import utilization as routes

NOW = datetime(2026, 11, 24, 5, tzinfo=UTC)


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(
            text("""CREATE TABLE cost_utilization_daily (
            id INTEGER PRIMARY KEY, vendor TEXT, account_id TEXT, usage_date DATE, week_start DATE,
            expires_at DATETIME, scope TEXT, resource_key TEXT, resource_class TEXT, resource_spec TEXT,
            repo TEXT, job_name TEXT, namespace TEXT, region TEXT, metrics_json JSON,
            cutoff DATETIME, evidence_cutoff DATETIME, status TEXT, updated_at DATETIME)""")
        )
        connection.execute(
            text("""CREATE TABLE cost_utilization_weekly (
            id INTEGER PRIMARY KEY, vendor TEXT, account_id TEXT, week_start DATE,
            resource_class TEXT, metric TEXT, contract_json JSON, values_json JSON,
            status TEXT, quality_checkpoint_at DATETIME, finalized_at DATETIME, updated_at DATETIME)
        """)
        )
        connection.execute(
            text("CREATE TABLE cost_job_state (job_name TEXT PRIMARY KEY, watermark_json JSON)")
        )
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


def metric(avg=25, denominator="requests"):
    return {
        "contract": {
            "namespace": "QCE/TKE2",
            "metric": "cpu",
            "period": 60,
            "source_stat": "avg",
            "unit": "cores",
            "normalized_unit": "cores",
            "semantic_version": "v1",
            "denominator": denominator,
        },
        "used_integral": 50,
        "capacity_integral": 200,
        "total_used_integral": 70,
        "expected_capacity_integral": 300,
        "expected_seconds": 60,
        "observed_seconds": 40,
        "avg": avg,
        "peak": 143.12,
        "peak_at": "2026-11-24T04:00:00Z",
        "coverage": 0.5,
        "peak_coverage": 1,
        "latest": None,
        "as_of": "2026-11-24T04:00:00Z",
        "quality": ["unknown_requests"],
    }


def insert_daily(
    engine,
    *,
    key="job",
    scope="job",
    repo="unknown_repo",
    job="unknown_job",
    account=queries.ACCOUNT_ID,
    expires="2026-12-06 16:00:00",
    usage="2026-11-24",
):
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO cost_utilization_daily
            (vendor,account_id,usage_date,week_start,expires_at,scope,resource_key,resource_class,
             resource_spec,repo,job_name,namespace,region,metrics_json,cutoff,evidence_cutoff,
             status,updated_at) VALUES
            ('tencent',:account,:usage,'2026-11-23',:expires,:scope,:key,'tke_native','2c/4GiB',
             :repo,:job,'jenkins-tidb','ap-beijing',:metrics,'2026-11-24 04:00:00',
             '2026-11-24 03:00:00','partial','2026-11-24 04:01:00')"""),
            {
                "account": account,
                "usage": usage,
                "expires": expires,
                "scope": scope,
                "key": key,
                "repo": repo,
                "job": job,
                "metrics": json.dumps(
                    {"cpu": metric(None), "memory": metric(0), "disk": {"avg": 99}}
                ),
            },
        )


def insert_weekly(engine, *, metric_name="cpu", status="final_incomplete"):
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO cost_utilization_weekly
            (vendor,account_id,week_start,resource_class,metric,contract_json,values_json,status,
             quality_checkpoint_at,finalized_at,updated_at) VALUES
            ('tencent',:account,'2026-11-16','tke_native',:metric,:contract,:values,:status,
             '2026-11-24 00:00:00',NULL,'2026-11-24 00:00:00')"""),
            {
                "account": queries.ACCOUNT_ID,
                "metric": metric_name,
                "contract": json.dumps(metric(denominator="physical")["contract"]),
                "values": json.dumps(metric(None, "physical")),
                "status": status,
            },
        )


def configure(engine, **extra):
    state = {
        "version": 1,
        "enabled": True,
        "collection_start_week": "2026-11-16",
        "contracts": {"tke_native": {"cpu": {"contract": metric()["contract"]}}},
        "maintenance_at": "2026-11-24T04:00:00",
        "paused": True,
        "pause_reasons": ["budget"],
        **extra,
    }
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO cost_job_state VALUES (:key,:state)"),
            {"key": queries.STATE_KEY, "state": json.dumps(state)},
        )


def test_disabled_requires_no_database_or_migration(monkeypatch):
    monkeypatch.delenv("CI_DASHBOARD_UTILIZATION_ENABLED", raising=False)
    monkeypatch.setattr(routes, "get_engine", lambda: pytest.fail("disabled must not build engine"))
    with TestClient(create_app()) as client:
        for path in ("status", "daily", "weekly"):
            payload = client.get(f"/api/v1/utilization/{path}").json()
            assert payload["enabled"] is False
            assert payload["reason"] == "disabled"
            assert payload["available"] is False
    assert queries.get_daily(None)["reason"] == "disabled"
    assert queries.get_weekly(None)["reason"] == "disabled"


def test_daily_scope_filters_no_fanout_or_zero_fill(client, engine):
    for scope in queries.SCOPES:
        insert_daily(engine, key=scope, scope=scope)
    insert_daily(engine, key="other", account="another-account")
    insert_daily(engine, key="expired", expires="2026-11-24 04:00:00")
    insert_daily(engine, key="old", usage="2026-11-01")
    response = client.get(
        "/api/v1/utilization/daily",
        params={
            "scope": "job",
            "repo": "unknown_repo",
            "job_name": "unknown_job",
            "resource_class": "tke_native",
            "resource_spec": "2c/4GiB",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1
    row = payload["items"][0]
    assert row["scope"] == "job"
    assert row["repo"] == "unknown_repo" and row["job_name"] == "unknown_job"
    assert row["metrics"]["cpu"]["avg"] is None
    assert row["metrics"]["memory"]["avg"] == 0
    assert row["metrics"]["cpu"]["peak"] == 143.12
    assert row["metrics"]["cpu"]["total_used_integral"] == 70
    assert "disk" not in row["metrics"]
    assert row["evidence_cutoff"] == "2026-11-24T03:00:00+00:00"
    assert payload["cost"]["available"] is False
    assert payload["start_date"] == "2026-11-16"
    for scope in queries.SCOPES:
        params = {"scope": scope}
        if scope == "resource":
            params["resource_key"] = "resource"
        assert len(client.get("/api/v1/utilization/daily", params=params).json()["items"]) == 1
    assert (
        client.get("/api/v1/utilization/daily", params={"repo": "' OR 1=1 --"}).json()["items"]
        == []
    )


def test_daily_pagination_is_bounded(client, engine):
    for key in ("a", "b", "c"):
        insert_daily(engine, key=key)
    first = client.get("/api/v1/utilization/daily?page_size=2").json()
    second = client.get("/api/v1/utilization/daily?page_size=2&page=2").json()
    assert first["has_more"] is True and second["has_more"] is False
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert first["items"][0]["resource_key"] != second["items"][0]["resource_key"]


@pytest.mark.parametrize(
    "path,params",
    [
        ("daily", {"page": 101}),
        ("daily", {"page_size": 101}),
        ("daily", {"page": 0}),
        ("daily", {"scope": "person"}),
        ("daily", {"owner": "someone"}),
        ("daily", {"start_date": "2026-11-16"}),
        ("daily", {"start_date": "2026-11-15", "end_date": "2026-11-24"}),
        ("daily", {"start_date": "2026-11-24", "end_date": "2026-11-25"}),
        ("daily", {"start_date": "2026-11-24", "end_date": "2026-11-23"}),
        ("daily", {"start_date": "0001-01-01", "end_date": "2026-11-23"}),
        ("daily", {"scope": "account_class", "repo": "pingcap/tidb"}),
        ("daily", {"scope": "repo", "job_name": "stable"}),
        ("daily", {"scope": "repo", "resource_spec": "2c"}),
        ("daily", {"scope": "job", "resource_key": "node"}),
        ("daily", {"repo": "x" * 513}),
        ("daily", {"start_date": "not-a-date"}),
        ("weekly", {"repo": "pingcap/tidb"}),
        ("weekly", {"metric": "disk"}),
        ("weekly", {"start_date": "2025-01-01", "end_date": "2026-01-01"}),
        ("weekly", {"end_date": "2026-11-24"}),
        ("weekly", {"page_size": 0}),
        ("status", {"owner": "someone"}),
    ],
)
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


def test_beijing_week_boundary_and_calendar_year():
    # Sunday UTC has already become Monday in Beijing.
    assert queries.date_range(None, None, now=datetime(2026, 11, 22, 16, tzinfo=UTC)) == (
        date(2026, 11, 16),
        date(2026, 11, 23),
    )
    assert queries.date_range(date(2024, 2, 29), date(2025, 2, 27), weekly=True, now=NOW)
    with pytest.raises(ValueError):
        queries.date_range(date(2024, 2, 29), date(2025, 2, 28), weekly=True, now=NOW)


def test_weekly_passes_sufficient_statistics_and_final_missing(client, engine):
    insert_weekly(engine)
    insert_weekly(engine, metric_name="memory", status="final_missing")
    insert_weekly(engine, metric_name="disk", status="final_missing")
    assert len(client.get("/api/v1/utilization/weekly").json()["items"]) == 2
    payload = client.get(
        "/api/v1/utilization/weekly",
        params={"resource_class": "tke_native", "metric": "cpu", "page_size": 1},
    ).json()
    assert payload["scope"] == "account_class"
    assert len(payload["items"]) == 1 and payload["has_more"] is False
    row = payload["items"][0]
    assert row["contract"]["denominator"] == "physical"
    assert row["values"]["avg"] is None
    assert row["values"]["capacity_integral"] == 200
    assert row["status"] == "final_incomplete"
    assert row["finalized_at"] is None
    all_rows = client.get("/api/v1/utilization/weekly?page_size=1").json()
    assert all_rows["has_more"] is True
    assert (
        client.get("/api/v1/utilization/weekly?metric=memory").json()["items"][0]["status"]
        == "final_missing"
    )


def test_status_reports_unconfigured_and_quality_without_mixing_scopes(client, engine):
    empty = client.get("/api/v1/utilization/status").json()
    assert empty["configured"] is False and empty["reason"] == "unconfigured"
    assert empty["stale"] is True and empty["missing_daily_dates"] is None
    configure(engine, collection_stop_week="2026-11-16")
    insert_daily(engine)
    insert_daily(engine, scope="account_class", key="account", usage="2026-11-16")
    insert_weekly(engine)
    payload = client.get("/api/v1/utilization/status").json()
    assert payload["configured"] is True and payload["collection_enabled"] is True
    assert payload["paused"] is True and payload["pause_reasons"] == ["budget"]
    assert payload["stale"] is False and payload["maintenance_stale"] is False
    assert payload["missing_daily_dates"] == [f"2026-11-{day}" for day in range(17, 23)]
    assert len(payload["quality"]) == 2
    assert payload["quality"][1]["unknown_repo_rows"] == 1
    assert payload["collection_gaps"][0]["status"] == "final_incomplete"


def test_missing_migration_is_informative(client, engine):
    with engine.begin() as connection:
        for table in ("cost_utilization_daily", "cost_utilization_weekly", "cost_job_state"):
            connection.execute(text(f"DROP TABLE {table}"))
    for path in ("daily", "weekly", "status"):
        response = client.get(f"/api/v1/utilization/{path}")
        assert response.status_code == 200
        assert response.json()["reason"] == "missing_schema"
    assert client.get("/healthz").status_code == 200


def test_database_construction_failure_is_informative(monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")

    def fail():
        raise ValueError("missing DB config")

    monkeypatch.setattr(routes, "get_engine", fail)
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/utilization/status").json()["reason"] == "database_unavailable"
    for function in (queries.get_daily, queries.get_weekly, queries.get_status):
        assert function(None)["reason"] == "database_unavailable"


def test_database_read_failure_is_not_reported_as_missing_schema(client, engine):
    def fail(*_args):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("SELECT", {}, Exception("connection refused"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        assert client.get("/api/v1/utilization/status").json()["reason"] == "database_unavailable"
    finally:
        event.remove(engine, "before_cursor_execute", fail)


@pytest.mark.parametrize(
    "function,params",
    [
        (queries.get_daily, {"scope": "bogus"}),
        (queries.get_daily, {"page": 101}),
        (queries.get_daily, {"repo": "x" * 513}),
        (queries.get_weekly, {"metric": "disk"}),
        (queries.get_weekly, {"page": 101}),
        (queries.get_weekly, {"resource_class": "x" * 513}),
    ],
)
def test_direct_query_validation(engine, monkeypatch, function, params):
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")
    with pytest.raises(ValueError):
        function(engine, **params)


def test_utilization_keeps_existing_company_auth_gate(monkeypatch):
    from ci_dashboard.api.auth import get_identity_verifier

    monkeypatch.delenv("CI_DASHBOARD_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("CI_DASHBOARD_OIDC_CLIENT_ID", raising=False)
    get_identity_verifier.cache_clear()
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/utilization/status").status_code == 503
