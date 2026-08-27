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


def test_resource_serving_expands_grouped_kubernetes_lineage() -> None:
    rows = build_resource_serving_rows(
        source_rows=(_source(source_summary_row_hash=None, source_fact_hash="group-source"),),
        detail_rows=(
            {
                "source_summary_row_hash": "summary-a", "resource_name": "instance-a",
                "parent_resource_name": None, "service_name": "Compute Engine",
                "vendor_tags_json": None, "usage_seconds": Decimal("60"), "list_cost": Decimal("60"),
            },
            {
                "source_summary_row_hash": "summary-b", "resource_name": "instance-b",
                "parent_resource_name": None, "service_name": "Compute Engine",
                "vendor_tags_json": None, "usage_seconds": Decimal("40"), "list_cost": Decimal("40"),
            },
        ),
        group_lineage={
            "group-source": (
                {"source_summary_row_hash": "summary-a", "source_list_cost": Decimal("60")},
                {"source_summary_row_hash": "summary-b", "source_list_cost": Decimal("40")},
            )
        },
        basis_key="kubernetes_allocated",
        materialization_version="v1",
        calculated_at=datetime(2026, 8, 11),
    )

    assert [(row["resource_name"], row["list_cost"]) for row in rows] == [
        ("instance-a", Decimal("60")),
        ("instance-b", Decimal("40")),
    ]
    assert all(row["fallback_list_cost"] == 0 for row in rows)


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
                  ('2026-08-10', 'gcp', 'project-1', 'summary-1', 'instance-1', NULL,
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


_SCHEMA = (
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
      resource_name TEXT, parent_resource_name TEXT, service_name TEXT, vendor_tags_json TEXT,
      usage_seconds REAL, list_cost REAL, source_row_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_resource_serving_daily (
      id INTEGER PRIMARY KEY AUTOINCREMENT, materialization_version TEXT, basis_key TEXT,
      usage_date TEXT, vendor TEXT, account_id TEXT, owner_key TEXT, owner TEXT,
      group_id INTEGER, manager_id INTEGER, target_branch TEXT, resource_group_key TEXT,
      resource_key TEXT, resource_name TEXT, service_name TEXT, resource_identity_kind TEXT,
      representative_labels_json TEXT, metadata_variant_count INTEGER, detail_list_cost REAL,
      fallback_list_cost REAL, usage_seconds REAL, list_cost REAL, effective_cost REAL,
      credit_amount REAL, net_cost REAL, source_row_count INTEGER, calculated_at TEXT
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
