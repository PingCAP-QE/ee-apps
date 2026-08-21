from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, text

from cost_insight.common.config import GcpBillingSettings
from cost_insight.common.gcp_summary_identity import build_gcp_summary_row_hash
from cost_insight.jobs import state_store
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_gcp_kubernetes_workload_allocations import (
    JOB_NAME,
    GkeNodeCost,
    GkeWorkloadUsage,
    _build_upsert_statement,
    build_gke_workload_allocation_rows,
    run_sync_gcp_kubernetes_workload_allocations,
)
from cost_insight.sources.gcp_gke_allocation import (
    build_gcp_gke_node_cost_query,
    build_gcp_gke_workload_usage_query,
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
              source_summary_row_hash TEXT,
              allocation_group_hash TEXT,
              calculated_at TEXT,
              updated_at TEXT,
              UNIQUE(usage_date, dimension_hash)
            )
            """,
            """
            CREATE TABLE cost_kubernetes_workload_allocation_source_daily (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              usage_date TEXT NOT NULL,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              source_summary_row_hash TEXT NOT NULL,
              allocation_group_hash TEXT NOT NULL,
              source_list_cost REAL NOT NULL,
              allocation_version TEXT NOT NULL,
              calculated_at TEXT,
              updated_at TEXT,
              UNIQUE(vendor, account_id, usage_date, source_summary_row_hash)
            )
            """,
        ):
            connection.execute(text(statement))
    return engine


def test_gke_allocation_upsert_updates_allocation_version() -> None:
    sqlite_statement = str(
        _build_upsert_statement(SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    )
    mysql_statement = str(
        _build_upsert_statement(SimpleNamespace(dialect=SimpleNamespace(name="mysql")))
    )

    assert "allocation_version = excluded.allocation_version" in sqlite_statement
    assert "allocation_version = VALUES(allocation_version)" in mysql_statement


def _node_cost_rows() -> list[dict[str, str | None]]:
    return [
        {
            "billing_account_id": "billing-1",
            "account_id": "pingcap-testing-account",
            "export_partition_date": "2026-08-10",
            "usage_date": "2026-08-10",
            "service_name": "Compute Engine",
            "sku_name": "N2 Instance Core running in Americas",
            "region": "us-central1",
            "author": None,
            "org": None,
            "repo": None,
            "target_branch": None,
            "resource_name": "projects/p/instances/gke-prow-a",
            "cluster_name": "prow",
            "cluster_location": "us-central1-c",
            "cost_component": "cpu",
            "list_cost": "100.00",
        },
        {
            "billing_account_id": "billing-1",
            "account_id": "pingcap-testing-account",
            "export_partition_date": "2026-08-10",
            "usage_date": "2026-08-10",
            "service_name": "Compute Engine",
            "sku_name": "N2 Instance Ram running in Americas",
            "region": "us-central1",
            "author": None,
            "org": None,
            "repo": None,
            "target_branch": None,
            "resource_name": "projects/p/instances/gke-prow-a",
            "cluster_name": "prow",
            "cluster_location": "us-central1-c",
            "cost_component": "memory",
            "list_cost": "50.00",
        },
        {
            "billing_account_id": "billing-1",
            "account_id": "pingcap-testing-account",
            "export_partition_date": "2026-08-10",
            "usage_date": "2026-08-10",
            "service_name": "Compute Engine",
            "sku_name": "Persistent Disk",
            "region": "us-central1",
            "author": None,
            "org": None,
            "repo": None,
            "target_branch": None,
            "resource_name": "pvc-123",
            "cluster_name": "prow",
            "cluster_location": "us-central1-c",
            "cost_component": "other",
            "list_cost": "15.00",
        },
        {
            "billing_account_id": "billing-1",
            "account_id": "pingcap-testing-account",
            "export_partition_date": "2026-08-10",
            "usage_date": "2026-08-10",
            "service_name": "Kubernetes Engine",
            "sku_name": "Kubernetes Engine Cluster Management Fee",
            "region": "us-central1",
            "author": None,
            "org": None,
            "repo": None,
            "target_branch": None,
            "resource_name": None,
            "cluster_name": None,
            "cluster_location": None,
            "cost_component": "control_plane",
            "list_cost": "5.00",
        },
    ]


def _workload_usage_rows() -> list[dict[str, str]]:
    return [
        {
            "usage_date": "2026-08-10",
            "cluster_name": "prow",
            "cluster_location": "us-central1-c",
            "namespace": "jenkins-tidb",
            "workload_name": "tidb-unit",
            "workload_type": "Jenkins agent",
            "author": "alice",
            "org": "pingcap",
            "repo": "tidb",
            "target_branch": "master",
            "cpu_seconds": "1",
            "memory_byte_seconds": "2",
        },
        {
            "usage_date": "2026-08-10",
            "cluster_name": "prow",
            "cluster_location": "us-central1-c",
            "namespace": "jenkins-tiflow",
            "workload_name": "ticdc-unit",
            "workload_type": "Jenkins agent",
            "author": "bob",
            "org": "pingcap",
            "repo": "ticdc",
            "target_branch": "master",
            "cpu_seconds": "3",
            "memory_byte_seconds": "2",
        },
    ]


def test_gke_allocation_splits_grouped_cpu_and_memory_and_records_source_lineage() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account", page_size=2)
    try:
        result = run_sync_gcp_kubernetes_workload_allocations(
            engine,
            settings=settings,
            usage_start_date=date(2026, 8, 10),
            usage_end_date=date(2026, 8, 10),
            node_cost_fetcher=lambda **_kwargs: _node_cost_rows(),
            workload_usage_fetcher=lambda **_kwargs: _workload_usage_rows(),
        )

        assert result.node_cost_rows_seen == 4
        assert result.metering_rows_seen == 2
        assert result.rows_written == 4
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT allocation_scope, cost_component, workload_name, list_cost,
                           source_summary_row_hash, allocation_group_hash
                    FROM cost_kubernetes_workload_allocation_daily
                    ORDER BY allocation_scope, cost_component, workload_name
                    """
                )
            ).all()
            totals = connection.execute(
                text(
                    """
                    SELECT allocation_scope, SUM(list_cost)
                    FROM cost_kubernetes_workload_allocation_daily
                    GROUP BY allocation_scope
                    ORDER BY allocation_scope
                    """
                )
            ).all()
            source_rows = connection.execute(
                text(
                    """
                    SELECT cost_component.cost_component, source.source_list_cost,
                           source.allocation_group_hash, source.source_summary_row_hash
                    FROM cost_kubernetes_workload_allocation_source_daily source
                    JOIN cost_kubernetes_workload_allocation_daily cost_component
                      ON cost_component.allocation_group_hash = source.allocation_group_hash
                    GROUP BY cost_component.cost_component, source.source_list_cost,
                             source.allocation_group_hash, source.source_summary_row_hash
                    ORDER BY cost_component.cost_component
                    """
                )
            ).all()
            state = state_store.get_job_state(
                connection,
                source_job_name(JOB_NAME, vendor="gcp", account_id=settings.account_id),
            )
        assert rows == [
            ("workload_split", "cpu", "ticdc-unit", 75.0, None, rows[0][5]),
            ("workload_split", "cpu", "tidb-unit", 25.0, None, rows[1][5]),
            ("workload_split", "memory", "ticdc-unit", 25.0, None, rows[2][5]),
            ("workload_split", "memory", "tidb-unit", 25.0, None, rows[3][5]),
        ]
        expected_source_hashes = {
            row["cost_component"]: build_gcp_summary_row_hash({"vendor": "gcp", **row})
            for row in _node_cost_rows()[:2]
        }
        assert {row[0]: row[3] for row in source_rows} == expected_source_hashes
        assert {row[0]: row[1] for row in source_rows} == {"cpu": 100.0, "memory": 50.0}
        assert len({row[2] for row in source_rows}) == 2
        assert totals == [("workload_split", 150.0)]
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_gke_allocation_does_not_replace_cost_without_metering() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    try:
        result = run_sync_gcp_kubernetes_workload_allocations(
            engine,
            settings=settings,
            usage_start_date=date(2026, 8, 10),
            usage_end_date=date(2026, 8, 10),
            dry_run=False,
            node_cost_fetcher=lambda **_kwargs: _node_cost_rows()[:2],
            workload_usage_fetcher=lambda **_kwargs: [],
        )

        assert result.rows_written == 0
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM cost_kubernetes_workload_allocation_daily
                    ORDER BY cost_component
                    """
                )
            ).all()
        assert rows == [(0,)]
    finally:
        engine.dispose()


def test_gke_allocation_weights_reconcile_after_quantization() -> None:
    node_cost = GkeNodeCost(
        usage_date=date(2026, 8, 10),
        source_summary_row_hash="source-cpu",
        cluster_name="prow",
        cluster_location="us-central1-c",
        cost_component="cpu",
        list_cost=Decimal("1.00"),
    )
    workloads = tuple(
        GkeWorkloadUsage(
            usage_date=node_cost.usage_date,
            cluster_name="prow",
            cluster_location="us-central1-c",
            namespace="jenkins",
            workload_name=f"job-{index}",
            workload_type="Job",
            author=None,
            org=None,
            repo=None,
            target_branch=None,
            cpu_seconds=Decimal(1),
            memory_byte_seconds=Decimal(),
        )
        for index in range(3)
    )

    rows, source_rows = build_gke_workload_allocation_rows(
        account_id="pingcap-testing-account",
        node_costs=(node_cost,),
        workload_usage=workloads,
    )

    assert [row["list_cost"] for row in rows] == [
        Decimal("0.33"),
        Decimal("0.33"),
        Decimal("0.34"),
    ]
    assert sum((row["allocation_weight"] for row in rows), Decimal()) == Decimal(1)
    assert len(source_rows) == 1


def test_gke_allocation_uses_grouped_facts_instead_of_source_workload_cross_product() -> None:
    node_costs = tuple(
        GkeNodeCost(
            usage_date=date(2026, 8, 10),
            source_summary_row_hash=f"source-{index}",
            cluster_name="prow",
            cluster_location="us-central1-c",
            cost_component="cpu",
            list_cost=Decimal("1.00"),
        )
        for index in range(100)
    )
    workloads = tuple(
        GkeWorkloadUsage(
            usage_date=date(2026, 8, 10),
            cluster_name="prow",
            cluster_location="us-central1-c",
            namespace="jenkins",
            workload_name=f"job-{index}",
            workload_type="Job",
            author=None,
            org=None,
            repo=None,
            target_branch=None,
            cpu_seconds=Decimal(1),
            memory_byte_seconds=Decimal(),
        )
        for index in range(100)
    )

    rows, source_rows = build_gke_workload_allocation_rows(
        account_id="pingcap-testing-account",
        node_costs=node_costs,
        workload_usage=workloads,
    )

    assert len(rows) == 100
    assert len(source_rows) == 100
    assert sum((row["list_cost"] for row in rows), Decimal()) == Decimal("100.00")
    assert len({row["allocation_group_hash"] for row in rows}) == 1


def test_gke_allocation_queries_use_positive_gke_cost_signals_and_metering_dimensions() -> None:
    node_query = build_gcp_gke_node_cost_query(billing_table="project.dataset.billing")
    usage_query = build_gcp_gke_workload_usage_query(gke_usage_table="project.dataset.gke_usage")

    assert "goog-k8s-cluster-name" in node_query
    assert "billing_account_id" in node_query
    assert "export_partition_date" in node_query
    assert "AS billing_cluster_name" in node_query
    assert "AS billing_cluster_location" in node_query
    assert "NULLIF(billing_cluster_name, '') IS NOT NULL" in node_query
    assert "STRUCT(billing_cluster_name, billing_cluster_location)" in node_query
    assert "resource.global_name AS raw_global_name" in node_query
    assert "NULLIF(billing_cluster_name, '') IS NOT NULL" in node_query
    assert "STARTS_WITH(LOWER(COALESCE(raw_resource_name, '')), 'pvc-')" in node_query
    assert "REGEXP_CONTAINS(LOWER(COALESCE(raw_resource_name, '')), r'/instances/gke-')" in node_query
    assert "REGEXP_CONTAINS(LOWER(COALESCE(raw_global_name, '')), r'/instances/gke-')" in node_query
    assert "service_name = 'Compute Engine'" in node_query
    assert "service_name = 'Kubernetes Engine'" not in node_query
    assert "Compute Flexible Committed Use Discounts" in node_query
    assert "project.id = @account_id" in usage_query
    assert "_PARTITIONDATE BETWEEN @usage_start_date AND DATE_ADD(@usage_end_date, INTERVAL 1 DAY)" in usage_query
    assert "resource_name IN ('cpu', 'memory')" in usage_query
    assert "CASE label.key WHEN 'author' THEN 0" in usage_query
    assert "ARRAY_POSITION" not in usage_query
