from datetime import date

from sqlalchemy import create_engine, text

from cost_insight.jobs.sync_aws_billing_summary import (
    AWS_SPLIT_COST_SCHEMA_VERSION,
    AwsBillingSource,
)
from cost_insight.jobs.sync_aws_parent_residual_allocations import (
    run_sync_aws_parent_residual_allocations,
)


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE cost_aws_parent_residual_allocation_daily (
                  usage_date TEXT NOT NULL,
                  vendor TEXT NOT NULL,
                  account_id TEXT NOT NULL,
                  parent_resource_id TEXT NOT NULL,
                  pod_resource_id TEXT NOT NULL,
                  namespace TEXT,
                  workload_name TEXT,
                  workload_type TEXT,
                  owner TEXT,
                  service TEXT,
                  project TEXT,
                  service_exec_id TEXT,
                  source_pod_split_list_cost REAL NOT NULL,
                  parent_direct_list_cost REAL NOT NULL,
                  parent_residual_list_cost REAL NOT NULL,
                  allocation_weight REAL NOT NULL,
                  derived_parent_residual_list_cost REAL NOT NULL,
                  allocation_origin TEXT NOT NULL,
                  allocation_method TEXT NOT NULL,
                  allocation_version TEXT NOT NULL,
                  parent_input_hash TEXT NOT NULL,
                  calculated_at TEXT,
                  updated_at TEXT,
                  UNIQUE(usage_date, vendor, account_id, parent_resource_id, pod_resource_id, allocation_version)
                )
                """
            )
        )
    return engine


def _ledger_row(pod_resource_id: str, split_cost: str) -> dict[str, str]:
    return {
        "vendor": "aws",
        "usage_date": "2026-08-10",
        "account_id": "946646677266",
        "parent_resource_id": "i-0ef88ef97606efb63",
        "pod_resource_id": pod_resource_id,
        "namespace": "ns",
        "workload_name": "workload",
        "workload_type": "deployment",
        "owner": "owner@pingcap.com",
        "service": "svc",
        "project": "project",
        "service_exec_id": "exec",
        "source_pod_split_list_cost": split_cost,
        "parent_direct_list_cost": "10.00",
        "parent_residual_list_cost": "4.00",
    }


def test_residual_ledger_excludes_zero_split_pods_and_replaces_usage_dates(caplog) -> None:
    engine = _sqlite_engine()
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 8, 2),
    )
    rows = [
        _ledger_row(":pod/a", "2.00000000"),
        _ledger_row(":pod/b", "4.00000000"),
        _ledger_row(":pod/zero-request", "0.00000000"),
    ]
    try:
        result = run_sync_aws_parent_residual_allocations(
            engine,
            source=source,
            usage_start_date=date(2026, 8, 10),
            usage_end_date=date(2026, 8, 10),
            export_partition_start=date(2026, 8, 1),
            export_partition_end=date(2026, 8, 1),
            page_size=10,
            fetch_rows=lambda **_kwargs: rows,
        )

        assert result.rows_seen == 3
        assert result.rows_written == 2
        assert result.parent_days == 1
        with engine.begin() as connection:
            persisted = connection.execute(
                text(
                    """
                    SELECT pod_resource_id, source_pod_split_list_cost,
                           derived_parent_residual_list_cost, allocation_origin, allocation_method
                    FROM cost_aws_parent_residual_allocation_daily
                    ORDER BY pod_resource_id
                    """
                )
            ).all()
        assert persisted == [
            (":pod/a", 2.0, 1.33, "cost_insight_derived", "proportional_source_split_list_v1"),
            (":pod/b", 4.0, 2.67, "cost_insight_derived", "proportional_source_split_list_v1"),
        ]

        with caplog.at_level("INFO"):
            dry_run = run_sync_aws_parent_residual_allocations(
                engine,
                source=source,
                usage_start_date=date(2026, 8, 10),
                usage_end_date=date(2026, 8, 10),
                export_partition_start=date(2026, 8, 1),
                export_partition_end=date(2026, 8, 1),
                page_size=10,
                dry_run=True,
                fetch_rows=lambda **_kwargs: rows,
            )
        assert dry_run.rows_written == 0
        assert "dry-run skipped parent residual allocation replacement" in caplog.text

        empty = run_sync_aws_parent_residual_allocations(
            engine,
            source=source,
            usage_start_date=date(2026, 8, 10),
            usage_end_date=date(2026, 8, 10),
            export_partition_start=date(2026, 8, 1),
            export_partition_end=date(2026, 8, 1),
            page_size=10,
            fetch_rows=lambda **_kwargs: [],
        )
        assert empty.rows_written == 0
        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_aws_parent_residual_allocation_daily")
            ).scalar_one() == 2

        zero_split = run_sync_aws_parent_residual_allocations(
            engine,
            source=source,
            usage_start_date=date(2026, 8, 10),
            usage_end_date=date(2026, 8, 10),
            export_partition_start=date(2026, 8, 1),
            export_partition_end=date(2026, 8, 1),
            page_size=10,
            fetch_rows=lambda **_kwargs: [_ledger_row(":pod/zero-request", "0.00000000")],
        )
        assert zero_split.rows_written == 0
        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_aws_parent_residual_allocation_daily")
            ).scalar_one() == 0
    finally:
        engine.dispose()
