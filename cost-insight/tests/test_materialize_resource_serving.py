import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, text

from cost_insight.jobs.materialize_resource_serving import (
    build_resource_serving_rows,
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


def test_resource_serving_merges_different_roster_metadata_for_one_resource() -> None:
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

    assert len(rows) == 1
    assert rows[0]["list_cost"] == Decimal("30")
    assert rows[0]["source_row_count"] == 2




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
                   'Compute Engine', '{"cluster":"prow"}', 40, 40, 'detail-1')
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
      usage_date TEXT, vendor TEXT, account_id TEXT, service_name TEXT, sku_name TEXT,
      region TEXT, org TEXT, repo TEXT, target_branch TEXT, resource_name TEXT,
      vendor_tags_json TEXT, owner TEXT, group_id INTEGER, manager_id INTEGER,
      usage_seconds REAL, list_cost REAL, effective_cost REAL, credit_amount REAL,
      net_cost REAL, source_rows INTEGER, source_summary_row_hash TEXT, dimension_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_unmatched_resource_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, source_summary_row_hash TEXT,
      resource_name TEXT, resource_id TEXT, parent_resource_name TEXT, service_name TEXT, vendor_tags_json TEXT,
      usage_seconds REAL, list_cost REAL, source_row_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_resource_serving_daily (
      id INTEGER PRIMARY KEY AUTOINCREMENT, materialization_version TEXT, basis_key TEXT,
      usage_date TEXT, vendor TEXT, account_id TEXT, owner_key TEXT, owner TEXT,
      group_id INTEGER, manager_id INTEGER, target_branch TEXT, resource_group_key TEXT,
      resource_key TEXT, resource_name TEXT, resource_id TEXT, service_name TEXT, resource_identity_kind TEXT,
      representative_labels_json TEXT, metadata_variant_count INTEGER, detail_list_cost REAL,
      fallback_list_cost REAL, usage_seconds REAL, list_cost REAL, effective_cost REAL,
      credit_amount REAL, net_cost REAL, source_row_count INTEGER, calculated_at TEXT,
      UNIQUE (materialization_version, basis_key, vendor, account_id, usage_date,
              owner_key, resource_key, target_branch)
    )
    """,
    """
    CREATE TABLE cost_resource_serving_publication (
      basis_key TEXT, vendor TEXT, account_id TEXT, usage_date TEXT,
      active_materialization_version TEXT, source_allocation_version TEXT,
      detail_list_cost REAL, total_list_cost REAL, source_row_count INTEGER,
      published_at TEXT DEFAULT CURRENT_TIMESTAMP, tiflash_ready_at TEXT,
      PRIMARY KEY (basis_key, vendor, account_id, usage_date)
    )
    """,
)
