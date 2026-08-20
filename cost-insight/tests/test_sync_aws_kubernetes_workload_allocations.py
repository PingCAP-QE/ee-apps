from datetime import date

import pytest
from sqlalchemy import create_engine, text

from cost_insight.jobs.sync_aws_billing_summary import (
    AWS_SPLIT_COST_SCHEMA_VERSION,
    AwsBillingSource,
)
from cost_insight.jobs.sync_aws_kubernetes_workload_allocations import (
    build_aws_kubernetes_workload_allocation_rows,
    run_sync_aws_kubernetes_workload_allocations,
)


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in (
            """
            CREATE TABLE cost_sources (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              billing_account_id TEXT,
              display_name TEXT,
              source_table TEXT,
              source_schema_version TEXT,
              source_available_from TEXT,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(vendor, account_id)
            )
            """,
            """
            CREATE TABLE cost_job_state (
              job_name TEXT PRIMARY KEY,
              watermark_json TEXT,
              last_started_at TEXT,
              last_succeeded_at TEXT,
              last_status TEXT,
              last_error TEXT,
              updated_at TEXT
            )
            """,
            """
            CREATE TABLE cost_bq_export_summary_daily (
              usage_date TEXT NOT NULL,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              source_schema_version TEXT,
              source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
              service_name TEXT,
              region TEXT,
              vendor_tags_json TEXT,
              namespace TEXT,
              workload_name TEXT,
              workload_type TEXT,
              author TEXT,
              org TEXT,
              repo TEXT,
              target_branch TEXT,
              list_cost REAL NOT NULL
            )
            """,
            """
            CREATE TABLE cost_aws_parent_residual_allocation_daily (
              usage_date TEXT NOT NULL,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              namespace TEXT,
              workload_name TEXT,
              workload_type TEXT,
              owner TEXT,
              project TEXT,
              derived_parent_residual_list_cost REAL NOT NULL
            )
            """,
            """
            CREATE TABLE cost_kubernetes_workload_allocation_daily (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              usage_date TEXT NOT NULL,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              cluster_name TEXT,
              cluster_location TEXT,
              allocation_scope TEXT NOT NULL,
              cost_component TEXT NOT NULL,
              namespace TEXT,
              workload_name TEXT,
              workload_type TEXT,
              author TEXT,
              org TEXT,
              repo TEXT,
              target_branch TEXT,
              allocation_weight REAL NOT NULL,
              source_node_list_cost REAL NOT NULL,
              list_cost REAL NOT NULL,
              allocation_method TEXT NOT NULL,
              allocation_version TEXT NOT NULL,
              dimension_hash TEXT NOT NULL,
              calculated_at TEXT,
              updated_at TEXT,
              UNIQUE(usage_date, dimension_hash)
            )
            """,
        ):
            connection.execute(text(statement))
    return engine


def _source() -> AwsBillingSource:
    return AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 8, 2),
    )


def _insert_summary_rows(engine) -> None:
    common = {
        "usage_date": "2026-08-10",
        "vendor": "aws",
        "account_id": "946646677266",
        "source_schema_version": AWS_SPLIT_COST_SCHEMA_VERSION,
        "region": None,
        "vendor_tags_json": None,
        "namespace": None,
        "workload_name": None,
        "workload_type": None,
        "author": None,
        "org": None,
        "repo": None,
        "target_branch": None,
    }
    rows = (
        {
            **common,
            "source_allocation_scope": "eks_pod",
            "service_name": "AmazonEC2",
            "region": "us-west-2",
            "vendor_tags_json": '{"cluster":"prow","shared_pool":"prow"}',
            "namespace": "prow",
            "workload_name": "plank",
            "workload_type": "deployment",
            "author": "ci-bot",
            "org": "pingcap",
            "repo": "ee-apps",
            "target_branch": "main",
            "list_cost": 2509,
        },
        {
            **common,
            "source_allocation_scope": "eks_parent_residual",
            "service_name": "AmazonEC2",
            "list_cost": 3779,
        },
        {
            **common,
            "source_allocation_scope": "eks_unallocated",
            "service_name": "AmazonEC2",
            "list_cost": 771,
        },
        {
            **common,
            "source_allocation_scope": "direct",
            "service_name": "AmazonEKS",
            "list_cost": 248,
        },
        {
            **common,
            "source_allocation_scope": "direct",
            "service_name": "AmazonVPC",
            "vendor_tags_json": '{"cluster":"prow"}',
            "list_cost": 50,
        },
        {
            **common,
            "source_allocation_scope": "direct",
            "service_name": "AmazonEC2",
            "vendor_tags_json": '{"shared_pool":"not-proof-of-eks"}',
            "list_cost": 1000,
        },
        {
            **common,
            "source_allocation_scope": "direct",
            "service_name": "AmazonEC2",
            "list_cost": 468,
        },
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_bq_export_summary_daily (
                  usage_date, vendor, account_id, source_schema_version, source_allocation_scope,
                  service_name, region, vendor_tags_json, namespace, workload_name, workload_type,
                  author, org, repo, target_branch, list_cost
                ) VALUES (
                  :usage_date, :vendor, :account_id, :source_schema_version, :source_allocation_scope,
                  :service_name, :region, :vendor_tags_json, :namespace, :workload_name, :workload_type,
                  :author, :org, :repo, :target_branch, :list_cost
                )
                """
            ),
            rows,
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_aws_parent_residual_allocation_daily (
                  usage_date, vendor, account_id, namespace, workload_name, workload_type,
                  owner, project, derived_parent_residual_list_cost
                ) VALUES
                  ('2026-08-10', 'aws', '946646677266', 'prow', 'plank', 'deployment',
                   'ci-bot', 'ee-apps', 2000),
                  ('2026-08-10', 'aws', '946646677266', 'prow', 'plank', 'deployment',
                   'ci-bot', 'ee-apps', 1779)
                """
            )
        )


def test_sync_aws_kubernetes_workload_allocations_publishes_only_evidence_backed_costs() -> None:
    engine = _sqlite_engine()
    try:
        _insert_summary_rows(engine)

        result = run_sync_aws_kubernetes_workload_allocations(
            engine,
            source=_source(),
            usage_start_date=date(2026, 8, 10),
            usage_end_date=date(2026, 8, 10),
        )

        assert result.summary_rows_seen == 6
        assert result.residual_allocation_rows_seen == 2
        assert result.rows_written == 5
        with engine.begin() as connection:
            totals = connection.execute(
                text(
                    """
                    SELECT allocation_scope, cost_component, list_cost
                    FROM cost_kubernetes_workload_allocation_daily
                    ORDER BY allocation_scope, cost_component
                    """
                )
            ).all()
        assert totals == [
            ("unallocated", "cluster_adjacent", 50.0),
            ("unallocated", "control_plane", 248.0),
            ("unallocated", "pvc", 771.0),
            ("workload_split", "parent_residual", 3779.0),
            ("workload_split", "pod_split", 2509.0),
        ]
    finally:
        engine.dispose()


def test_sync_aws_kubernetes_workload_allocations_does_not_replace_when_residual_ledger_is_missing() -> None:
    engine = _sqlite_engine()
    try:
        _insert_summary_rows(engine)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM cost_aws_parent_residual_allocation_daily"))
        with pytest.raises(RuntimeError, match="residual allocation ledger is empty"):
            run_sync_aws_kubernetes_workload_allocations(
                engine,
                source=_source(),
                usage_start_date=date(2026, 8, 10),
                usage_end_date=date(2026, 8, 10),
            )
    finally:
        engine.dispose()


def test_build_aws_kubernetes_workload_allocation_rows_excludes_bare_shared_pool_cost() -> None:
    rows = build_aws_kubernetes_workload_allocation_rows(
        account_id="946646677266",
        summary_rows=[
            {
                "usage_date": "2026-08-10",
                "source_allocation_scope": "direct",
                "service_name": "AmazonEC2",
                "vendor_tags_json": '{"shared_pool":"shared-but-not-eks"}',
                "list_cost": "123.45",
            }
        ],
        residual_rows=[],
    )

    assert rows == ()


def test_build_aws_kubernetes_workload_allocation_rows_accepts_eks_namespace_before_resync() -> None:
    rows = build_aws_kubernetes_workload_allocation_rows(
        account_id="946646677266",
        summary_rows=[
            {
                "usage_date": "2026-08-10",
                "source_allocation_scope": "split_child",
                "service_name": "AmazonEC2",
                "namespace": "prow",
                "workload_name": "plank",
                "workload_type": "deployment",
                "list_cost": "123.45",
            }
        ],
        residual_rows=[],
    )

    assert len(rows) == 1
    assert rows[0]["allocation_scope"] == "workload_split"
    assert rows[0]["cost_component"] == "pod_split"
