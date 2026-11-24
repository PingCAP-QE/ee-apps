import json
import weakref
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text

from cost_insight.jobs import materialize_resource_serving as job
from cost_insight.jobs.materialize_resource_serving import (
    build_resource_serving_rows,
    build_tencent_resource_serving_rows,
    run_materialize_resource_serving,
)


def _source(**overrides):
    return {
        "usage_date": date(2026, 8, 10),
        "vendor": "gcp",
        "account_id": "project-1",
        "service_name": "Compute Engine",
        "resource_name": None,
        "owner": None,
        "group_id": 1,
        "manager_id": 10,
        "project": "prow",
        "target_branch": "master",
        "vendor_tags_json": '{"cluster":"prow"}',
        "source_fact_hash": "source-1",
        "source_summary_row_hash": "summary-1",
        "source_rows": 1,
        "usage_seconds": Decimal("100"),
        "list_cost": Decimal("100"),
        "effective_cost": Decimal("80"),
        "credit_amount": Decimal("-10"),
        "net_cost": Decimal("70"),
        **overrides,
    }


def test_resource_serving_retains_partial_detail_as_explicit_fallback() -> None:
    rows = build_resource_serving_rows(
        source_rows=(_source(),),
        detail_rows=(
            {
                "source_summary_row_hash": "summary-1",
                "resource_name": "instance-1",
                "parent_resource_name": None,
                "service_name": "Compute Engine",
                "vendor_tags_json": '{"cluster":"prow"}',
                "usage_seconds": Decimal("40"),
                "list_cost": Decimal("40"),
            },
        ),
        basis_key="native",
        materialization_version="v1",
        calculated_at=datetime(2026, 8, 11),
    )

    assert sum((row["list_cost"] for row in rows), Decimal()) == Decimal("100")
    assert sum((row["detail_list_cost"] for row in rows), Decimal()) == Decimal("40")
    assert sum((row["fallback_list_cost"] for row in rows), Decimal()) == Decimal("60")
    assert {row["resource_identity_kind"] for row in rows} == {
        "resource_detail",
        "attribution_fallback",
    }
    assert {row["owner"] for row in rows} == {""}


def test_resource_serving_keeps_team_scopes_separate_for_one_resource() -> None:
    rows = build_resource_serving_rows(
        source_rows=(
            _source(
                source_summary_row_hash="summary-a",
                source_fact_hash="source-a",
                group_id=None,
                manager_id=None,
                usage_seconds=Decimal("10"),
                list_cost=Decimal("10"),
                effective_cost=Decimal("10"),
                credit_amount=Decimal(),
                net_cost=Decimal("10"),
            ),
            _source(
                source_summary_row_hash="summary-b",
                source_fact_hash="source-b",
                group_id=229,
                manager_id=483,
                usage_seconds=Decimal("20"),
                list_cost=Decimal("20"),
                effective_cost=Decimal("20"),
                credit_amount=Decimal(),
                net_cost=Decimal("20"),
            ),
        ),
        detail_rows=(
            {
                "source_summary_row_hash": "summary-a",
                "resource_name": "instance-1",
                "parent_resource_name": None,
                "service_name": "Compute Engine",
                "vendor_tags_json": None,
                "usage_seconds": Decimal("10"),
                "list_cost": Decimal("10"),
            },
            {
                "source_summary_row_hash": "summary-b",
                "resource_name": "instance-1",
                "parent_resource_name": None,
                "service_name": "Compute Engine",
                "vendor_tags_json": None,
                "usage_seconds": Decimal("20"),
                "list_cost": Decimal("20"),
            },
        ),
        basis_key="native",
        materialization_version="v1",
        calculated_at=datetime(2026, 8, 11),
    )

    assert len(rows) == 2
    assert sum((row["list_cost"] for row in rows), Decimal()) == Decimal("30")
    assert len({row["resource_group_key"] for row in rows}) == 1
    assert len({row["resource_key"] for row in rows}) == 2
    assert {row["group_id"] for row in rows} == {None, 229}




def test_materialize_resource_serving_publishes_a_refreshed_zero_cost_window() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))
        connection.execute(
            text(
                """
                INSERT INTO cost_job_state (job_name, watermark_json, last_status)
                VALUES (:job_name, :watermark_json, 'succeeded')
                """
            ),
            {
                "job_name": "refresh_cost_attribution_from_summary:gcp:project-1",
                "watermark_json": json.dumps(
                    {
                        "vendor": "gcp",
                        "account_id": "project-1",
                        "start_date": "2026-08-10",
                        "end_date": "2026-08-10",
                    }
                ),
            },
        )

    summary = run_materialize_resource_serving(
        engine,
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        materialization_version="v1",
        now=datetime(2026, 8, 11),
    )

    with engine.begin() as connection:
        publication = connection.execute(
            text("SELECT source_row_count, total_list_cost FROM cost_resource_serving_publication")
        ).one()
    assert summary.windows_published == 1
    assert summary.rows_written == 0
    assert publication == (0, 0)


def test_source_scoped_materializer_keeps_unrefreshed_empty_dates_pending() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))

    initial = run_materialize_resource_serving(
        engine,
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        vendor="gcp",
        account_id="project-1",
        materialization_version="v1",
        now=datetime(2026, 8, 11),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_job_state (job_name, watermark_json, last_status)
                VALUES (:job_name, :watermark_json, 'succeeded')
                """
            ),
            {
                "job_name": "refresh_cost_attribution_from_summary:gcp:project-1",
                "watermark_json": json.dumps(
                    {
                        "vendor": "gcp",
                        "account_id": "project-1",
                        "start_date": "2026-08-10",
                        "end_date": "2026-08-10",
                    }
                ),
            },
        )

    refreshed = run_materialize_resource_serving(
        engine,
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        vendor="gcp",
        account_id="project-1",
        materialization_version="v2",
        now=datetime(2026, 8, 11),
    )

    with engine.begin() as connection:
        publication_count = connection.execute(
            text("SELECT COUNT(*) FROM cost_resource_serving_publication")
        ).scalar_one()
    assert initial.windows_published == 0
    assert refreshed.windows_published == 1
    assert publication_count == 1


def test_materialize_resource_serving_stages_and_publishes_native_window() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))
        connection.execute(
            text(
                """
                INSERT INTO cost_attribution_daily (
                  usage_date, vendor, account_id, service_name, target_branch, group_id, manager_id,
                  usage_seconds, list_cost, effective_cost, credit_amount, net_cost, source_rows,
                  source_summary_row_hash, dimension_hash
                ) VALUES (
                  '2026-08-10', 'gcp', 'project-1', 'Compute Engine', 'master', 1, 10,
                  100, 100, 80, -10, 70, 1, 'summary-1', 'source-1'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_unmatched_resource_daily VALUES
                  ('2026-08-10', 'gcp', 'project-1', 'summary-1', 'instance-1', NULL, NULL,
                   'Compute Engine', '{"cluster":"prow"}', 40, 40, 'USD', 'detail-1')
                """
            )
        )

    summary = run_materialize_resource_serving(
        engine,
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        materialization_version="v1",
        now=datetime(2026, 8, 11),
    )

    with engine.begin() as connection:
        published = connection.execute(
            text(
                """
                SELECT active_materialization_version, detail_list_cost, total_list_cost
                FROM cost_resource_serving_publication
                """
            )
        ).one()
        totals = connection.execute(
            text(
                """
                SELECT SUM(list_cost), SUM(detail_list_cost), SUM(fallback_list_cost)
                FROM cost_resource_serving_daily
                """
            )
        ).one()
    assert summary.windows_published == 1
    assert published == ("v1", 40, 100)
    assert totals == (100, 40, 60)


def test_resource_serving_keeps_provider_ids_and_fallback_identity_separate() -> None:
    sources = (
        _source(source_summary_row_hash="summary-a", source_fact_hash="fact-a", list_cost=Decimal("4")),
        _source(source_summary_row_hash="summary-b", source_fact_hash="fact-b", list_cost=Decimal("6")),
        _source(source_summary_row_hash="summary-c", source_fact_hash="fact-c", list_cost=Decimal("1")),
        _source(source_summary_row_hash="summary-d", source_fact_hash="fact-d", list_cost=Decimal("1")),
    )
    details = (
        {
            "source_summary_row_hash": "summary-a",
            "resource_id": "i-0123456789abcdef0",
            "resource_name": "i-0123456789abcdef0",
            "parent_resource_name": None,
            "service_name": "AmazonEC2",
            "vendor_tags_json": '{"Name":"runner-a"}',
            "usage_seconds": Decimal("10"),
            "list_cost": Decimal("4"),
        },
        {
            "source_summary_row_hash": "summary-b",
            "resource_id": "i-0123456789abcdef0",
            "resource_name": "i-0123456789abcdef0",
            "parent_resource_name": None,
            "service_name": "AmazonS3",
            "vendor_tags_json": '{"Name":"bucket-a"}',
            "usage_seconds": None,
            "list_cost": Decimal("6"),
        },
    )

    rows = build_resource_serving_rows(
        source_rows=sources,
        detail_rows=details,
        basis_key="native",
        materialization_version="v1",
        calculated_at=datetime(2026, 8, 11),
    )

    detail_rows = [row for row in rows if row["resource_identity_kind"] == "resource_detail"]
    fallback_rows = [row for row in rows if row["resource_identity_kind"] == "attribution_fallback"]
    assert {row["resource_id"] for row in detail_rows} == {"i-0123456789abcdef0"}
    assert len({row["resource_group_key"] for row in detail_rows}) == 1
    assert {row["resource_id"] for row in fallback_rows} == {None}
    assert len({row["resource_group_key"] for row in fallback_rows}) == 2


def test_materializer_excludes_gcp_flexible_cud_list_cost() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))
        connection.execute(
            text(
                """
                INSERT INTO cost_attribution_daily (
                  usage_date, vendor, account_id, service_name, sku_name, target_branch,
                  usage_seconds, list_cost, effective_cost, credit_amount, net_cost, source_rows,
                  source_summary_row_hash, dimension_hash
                ) VALUES (
                  '2026-08-10', 'gcp', 'project-1', 'Compute Engine',
                  'Compute Flexible Committed Use Discounts - 1 Year', 'master',
                  NULL, 12, 12, 0, 12, 1, 'summary-cud', 'fact-cud'
                )
                """
            )
        )

    run_materialize_resource_serving(
        engine,
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        materialization_version="v1",
        now=datetime(2026, 8, 11),
    )

    with engine.begin() as connection:
        total = connection.execute(
            text("SELECT total_list_cost FROM cost_resource_serving_publication")
        ).scalar_one()
        serving_total = connection.execute(
            text("SELECT SUM(list_cost) FROM cost_resource_serving_daily")
        ).scalar_one()
    assert total == 0
    assert serving_total == 0


_SCHEMA = (
    """
    CREATE TABLE cost_job_state (
      job_name TEXT PRIMARY KEY, watermark_json TEXT, last_status TEXT
    )
    """,
    """
    CREATE TABLE cost_attribution_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, service_name TEXT, service TEXT, sku_name TEXT,
      region TEXT, org TEXT, repo TEXT, project TEXT, target_branch TEXT, resource_name TEXT,
      vendor_tags_json TEXT, source_allocation_scope TEXT, owner TEXT, employee_id INTEGER,
      group_id INTEGER, manager_id INTEGER,
      usage_seconds REAL, list_cost REAL, effective_cost REAL, credit_amount REAL,
      net_cost REAL, currency TEXT NOT NULL DEFAULT 'USD',
      source_rows INTEGER, source_summary_row_hash TEXT, dimension_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_unmatched_resource_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, source_summary_row_hash TEXT,
      resource_name TEXT, resource_id TEXT, parent_resource_name TEXT, service_name TEXT, vendor_tags_json TEXT,
      usage_seconds REAL, list_cost REAL, currency TEXT NOT NULL DEFAULT 'USD', source_row_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_resource_serving_daily (
      id INTEGER PRIMARY KEY AUTOINCREMENT, materialization_version TEXT, basis_key TEXT,
      usage_date TEXT, vendor TEXT, account_id TEXT, owner_key TEXT, owner TEXT,
      group_id INTEGER, manager_id INTEGER, project TEXT, target_branch TEXT, resource_group_key TEXT,
      resource_key TEXT, resource_name TEXT, resource_id TEXT, service_name TEXT, resource_identity_kind TEXT,
      representative_labels_json TEXT, metadata_variant_count INTEGER, detail_list_cost REAL,
      fallback_list_cost REAL, usage_seconds REAL, list_cost REAL, effective_cost REAL,
      credit_amount REAL, net_cost REAL, currency TEXT NOT NULL DEFAULT 'USD', source_row_count INTEGER, calculated_at TEXT,
      UNIQUE (materialization_version, basis_key, vendor, account_id, usage_date,
              owner_key, resource_key, target_branch)
    )
    """,
    """
    CREATE TABLE cost_resource_serving_publication (
      basis_key TEXT, vendor TEXT, account_id TEXT, usage_date TEXT,
      active_materialization_version TEXT, source_allocation_version TEXT,
      detail_list_cost REAL, total_list_cost REAL, currency TEXT NOT NULL DEFAULT 'USD', source_row_count INTEGER,
      published_at TEXT DEFAULT CURRENT_TIMESTAMP, tiflash_ready_at TEXT,
      PRIMARY KEY (basis_key, vendor, account_id, usage_date)
    )
    """,
)


@pytest.mark.parametrize(
    "table",
    (
        "cost_attribution_daily",
        "cost_unmatched_resource_daily",
        "cost_resource_serving_daily",
        "cost_resource_serving_publication",
    ),
)
def test_serving_schema_requires_currency(table: str) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        with engine.begin() as connection:
            for statement in _SCHEMA:
                if f"CREATE TABLE {table}" in statement:
                    statement = statement.replace(
                        ", currency TEXT NOT NULL DEFAULT 'USD'", ""
                    )
                connection.execute(text(statement))
        assert not job._serving_schema_ready(engine)
    finally:
        engine.dispose()


def test_serving_identity_and_conservation_separate_currencies() -> None:
    usd = _source()
    cny = _source(currency="CNY")

    rows = build_resource_serving_rows(
        source_rows=(usd, cny),
        detail_rows=(),
        basis_key="native",
        materialization_version="v1",
        calculated_at=datetime(2026, 8, 11),
    )

    by_currency = {row["currency"]: row for row in rows}
    assert set(by_currency) == {"USD", "CNY"}
    assert by_currency["USD"]["resource_key"] != by_currency["CNY"]["resource_key"]
    assert by_currency["USD"]["resource_group_key"] != by_currency["CNY"]["resource_group_key"]
    assert sum((row["list_cost"] for row in rows), Decimal()) == Decimal("200")


def test_tencent_serving_keeps_same_project_services_separate() -> None:
    common = {
        "usage_date": date(2026, 8, 10),
        "vendor": "tencent",
        "account_id": "100050658403",
        "source_rows": 1,
        "target_branch": None,
        "group_id": 1,
        "manager_id": 10,
    }
    sources = (
        {
            **common,
            "source_allocation_scope": "tencent_ci_shared",
            "currency": "CNY",
            "service_name": "COS",
            "service": "bazel",
            "project": "cache-platform",
            "employee_id": 1,
            "owner": "alice@example.com",
            "org": "pingcap",
            "repo": "repo-a",
            "list_cost": 10,
            "effective_cost": 10,
            "credit_amount": None,
            "net_cost": 10,
            "dimension_hash": "a1",
        },
        {
            **common,
            "source_allocation_scope": "tencent_ci_shared",
            "currency": "CNY",
            "service_name": "COS",
            "service": "bazel",
            "project": "cache-platform",
            "employee_id": None,
            "owner": None,
            "org": None,
            "repo": None,
            "list_cost": 10,
            "effective_cost": 10,
            "credit_amount": None,
            "net_cost": 10,
            "dimension_hash": "a2",
        },
        {
            **common,
            "source_allocation_scope": "tencent_ci_shared",
            "currency": "CNY",
            "service_name": "COS",
            "service": "tikv",
            "project": "cache-platform",
            "employee_id": 2,
            "owner": "bob@example.com",
            "org": "pingcap",
            "repo": "repo-b",
            "list_cost": 30,
            "effective_cost": 30,
            "credit_amount": None,
            "net_cost": 30,
            "dimension_hash": "b1",
        },
        {
            **common,
            "source_allocation_scope": "tencent_ci_shared",
            "currency": "CNY",
            "service_name": "COS",
            "service": "tikv",
            "project": "cache-platform",
            "employee_id": None,
            "owner": None,
            "org": None,
            "repo": None,
            "list_cost": 30,
            "effective_cost": 30,
            "credit_amount": None,
            "net_cost": 30,
            "dimension_hash": "b2",
        },
    )
    summaries = (
        {
            "source_row_hash": "summary-a",
            "currency": "CNY",
            "service_name": "COS",
            "service": "bazel",
            "project": "cache-platform",
            "resource_name": "bucket-a",
            "vendor_tags_json": '{"service":"bazel"}',
            "list_cost": 10,
            "effective_cost": 10,
            "credit_amount": None,
            "net_cost": 10,
        },
        {
            "source_row_hash": "summary-b",
            "currency": "CNY",
            "service_name": "COS",
            "service": "tikv",
            "project": "cache-platform",
            "resource_name": "bucket-b",
            "vendor_tags_json": '{"service":"tikv"}',
            "list_cost": 30,
            "effective_cost": 30,
            "credit_amount": None,
            "net_cost": 30,
        },
    )

    rows = build_tencent_resource_serving_rows(
        source_rows=sources,
        summary_rows=summaries,
        basis_key="native",
        materialization_version="v1",
        calculated_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    by_resource_owner = {(row["resource_id"], row["owner"]): row for row in rows}
    assert by_resource_owner["bucket-a", "alice@example.com"]["net_cost"] == Decimal("5")
    assert by_resource_owner["bucket-a", ""]["net_cost"] == Decimal("5")
    assert by_resource_owner["bucket-b", "bob@example.com"]["net_cost"] == Decimal("15")
    assert by_resource_owner["bucket-b", ""]["net_cost"] == Decimal("15")


def _tencent_shared_window(resource_count=40, participant_count=12):
    sources = tuple(
        _source(
            vendor="tencent", account_id="100050658403", currency="CNY",
            service_name="CBS", service="cicd", project="cicd",
            source_allocation_scope="tencent_ci_shared", source_summary_row_hash=None,
            employee_id=index, owner=f"owner-{index}", org="pingcap", repo="tidb",
            source_fact_hash=f"participant-{index}",
            list_cost=Decimal(resource_count) / participant_count,
            effective_cost=Decimal(resource_count) / participant_count,
            credit_amount=None, net_cost=Decimal(resource_count) / participant_count,
        )
        for index in range(participant_count)
    )
    summaries = tuple(
        {
            "source_row_hash": f"summary-{index}", "currency": "CNY",
            "service_name": "CBS", "service": "cicd", "project": "cicd",
            "resource_name": f"disk-{index}", "vendor_tags_json": '{"service":"cicd"}',
            "list_cost": Decimal(1), "effective_cost": Decimal(1),
            "credit_amount": None, "net_cost": Decimal(1),
        }
        for index in range(resource_count)
    )
    return sources, summaries


def test_tencent_serving_does_not_retain_the_resource_participant_product(monkeypatch) -> None:
    sources, summaries = _tencent_shared_window()
    original = job._tencent_resource_contribution
    live = weakref.WeakValueDictionary()
    peak = 0
    created = 0

    class Contribution(dict):
        pass

    def track(*args, **kwargs):
        nonlocal peak, created
        row = Contribution(original(*args, **kwargs))
        created += 1
        live[created] = row
        peak = max(peak, len(live))
        return row

    monkeypatch.setattr(job, "_tencent_resource_contribution", track)
    count = 0
    totals = {amount: Decimal() for amount in job._AMOUNTS}
    for row in build_tencent_resource_serving_rows(
        source_rows=sources, summary_rows=summaries, basis_key="native",
        materialization_version="v1", calculated_at=datetime(2026, 8, 11, tzinfo=UTC),
    ):
        count += 1
        for amount in totals:
            totals[amount] += row[amount] or Decimal()
    assert count == created == 40 * 12
    assert totals == {
        "list_cost": Decimal(40), "effective_cost": Decimal(40),
        "credit_amount": Decimal(), "net_cost": Decimal(40),
    }
    # A single resource's participants may be live, never the whole day's product.
    assert peak <= 24


@pytest.mark.parametrize("resource_name", ("disk-0", None))
def test_tencent_streaming_merges_labels_and_pools_before_yielding(resource_name) -> None:
    sources, summaries = _tencent_shared_window(resource_count=1, participant_count=1)
    sources = (
        sources[0],
        {**sources[0], "service": "other", "source_fact_hash": "other"},
        {
            **sources[0], "source_allocation_scope": "direct",
            "source_summary_row_hash": "direct", "source_fact_hash": "direct",
        },
    )
    summaries = (
        {**summaries[0], "resource_name": resource_name},
        {
            **summaries[0], "source_row_hash": "other", "service": "other",
            "resource_name": resource_name, "vendor_tags_json": '{"label":"other"}',
        },
        {
            **summaries[0], "source_row_hash": "direct", "resource_name": resource_name,
            "vendor_tags_json": '{"label":"direct"}',
        },
    )
    rows = tuple(build_tencent_resource_serving_rows(
        source_rows=sources, summary_rows=summaries, basis_key="native",
        materialization_version="v1", calculated_at=datetime(2026, 8, 11, tzinfo=UTC),
    ))
    job._assert_conserved(sources, rows)
    assert sum(row["net_cost"] for row in rows) == 3
    if resource_name:
        assert len(rows) == 1
        assert rows[0]["metadata_variant_count"] == 3
        assert rows[0]["source_row_count"] == 3
        assert rows[0]["representative_labels_json"] == '{"service":"cicd"}'
    else:
        assert len(rows) == 3
        assert len({row["resource_key"] for row in rows}) == 3
        assert all(row["resource_identity_kind"] == "attribution_fallback" for row in rows)


@pytest.mark.parametrize("failure", (None, "conservation", "iteration", "insert", "transport", "dry_run"))
def test_streamed_staging_validates_before_publication(monkeypatch, failure) -> None:
    from pymysql.err import OperationalError

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    sources, summaries = _tencent_shared_window(resource_count=4, participant_count=3)
    window = {"usage_date": sources[0]["usage_date"], "vendor": "tencent", "account_id": "100050658403"}
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))
    monkeypatch.setattr(job, "_source_windows", lambda *args, **kwargs: (window,))
    monkeypatch.setattr(job, "_load_source_rows", lambda *args, **kwargs: sources)
    monkeypatch.setattr(job, "_load_tencent_summary_rows", lambda *args, **kwargs: summaries)
    options = {
        "start_date": window["usage_date"], "end_date": window["usage_date"],
        "vendor": "tencent", "account_id": window["account_id"], "batch_size": 2,
    }
    try:
        run_materialize_resource_serving(engine, materialization_version="old", **options)
        original = job.build_tencent_resource_serving_rows
        produced = 0
        inserts = 0

        def stream(**kwargs):
            nonlocal produced
            for row in original(**kwargs):
                produced += 1
                if failure == "conservation":
                    row["net_cost"] += 1
                yield row
            if failure == "iteration":
                raise RuntimeError("generation failed after the last row")

        def observe_insert(connection, cursor, statement, parameters, context, executemany):
            nonlocal inserts
            if statement.lstrip().startswith("INSERT INTO cost_resource_serving_daily"):
                inserts += 1
                # The first batch must reach SQL before the remaining product is built.
                assert produced == inserts * 2
                if failure == "insert" and inserts == 2:
                    raise RuntimeError("insert failed")
                if failure == "transport" and inserts == 2:
                    raise OperationalError(2013, "Lost connection during staging")

        monkeypatch.setattr(job, "build_tencent_resource_serving_rows", stream)
        event.listen(engine, "before_cursor_execute", observe_insert)
        if failure in ("conservation", "iteration", "insert", "transport"):
            with pytest.raises((RuntimeError, OperationalError)):
                run_materialize_resource_serving(engine, materialization_version="new", **options)
        else:
            result = run_materialize_resource_serving(
                engine, materialization_version="new", dry_run=failure == "dry_run", **options,
            )
            assert result.rows_written == (0 if failure else 12)
            assert result.windows_published == (0 if failure else 1)
        with engine.connect() as connection:
            version = connection.execute(text(
                "SELECT active_materialization_version FROM cost_resource_serving_publication"
            )).scalar_one()
            staged_count = connection.execute(text(
                "SELECT COUNT(*) FROM cost_resource_serving_daily WHERE materialization_version='new'"
            )).scalar_one()
        assert version == ("old" if failure else "new")
        assert staged_count == (0 if failure else 12)
        assert inserts == (0 if failure == "dry_run" else 2 if failure in ("insert", "transport") else 6)
    finally:
        engine.dispose()


def test_serving_rejects_detail_currency_mismatch() -> None:
    with pytest.raises(RuntimeError, match="currency differs"):
        build_resource_serving_rows(
            source_rows=(_source(),),
            detail_rows=(
                {
                    "source_summary_row_hash": "summary-1",
                    "resource_name": "instance-1",
                    "parent_resource_name": None,
                    "service_name": "Compute Engine",
                    "vendor_tags_json": None,
                    "usage_seconds": Decimal("40"),
                    "list_cost": Decimal("40"),
                    "currency": "CNY",
                },
            ),
            basis_key="native",
            materialization_version="v1",
            calculated_at=datetime(2026, 8, 11),
        )
