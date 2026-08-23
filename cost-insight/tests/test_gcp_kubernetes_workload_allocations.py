from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from cost_insight.common.config import GcpBillingSettings
from cost_insight.jobs import sync_gcp_kubernetes_workload_allocations as gke_sync
from cost_insight.jobs.sync_gcp_kubernetes_workload_allocations import (
    ALLOCATION_VERSION,
    build_gke_workload_allocation_rows,
    run_sync_gcp_kubernetes_workload_allocations,
)
from cost_insight.sources.gcp_billing_export import build_gcp_billing_summary_query


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in (
            """
            CREATE TABLE cost_sources (
              id INTEGER PRIMARY KEY AUTOINCREMENT, vendor TEXT NOT NULL, account_id TEXT NOT NULL,
              billing_account_id TEXT, display_name TEXT, source_table TEXT,
              source_schema_version TEXT, source_available_from TEXT, is_active INTEGER DEFAULT 1,
              created_at TEXT, updated_at TEXT, UNIQUE(vendor, account_id)
            )
            """,
            """
            CREATE TABLE cost_job_state (
              job_name TEXT PRIMARY KEY, watermark_json TEXT, last_started_at TEXT,
              last_succeeded_at TEXT, last_status TEXT, last_error TEXT, updated_at TEXT
            )
            """,
            """
            CREATE TABLE cost_bq_export_summary_daily (
              usage_date TEXT NOT NULL, vendor TEXT NOT NULL, account_id TEXT NOT NULL,
              service_name TEXT, sku_name TEXT, source_allocation_scope TEXT,
              cluster_name TEXT, cluster_location TEXT, kubernetes_cost_class TEXT,
              kubernetes_residual_type TEXT, kubernetes_cost_component TEXT,
              namespace TEXT, workload_name TEXT, workload_type TEXT, author TEXT,
              org TEXT, repo TEXT, target_branch TEXT, list_cost REAL,
              effective_cost REAL, credit_amount REAL, net_cost REAL, source_row_hash TEXT
            )
            """,
            """
            CREATE TABLE cost_kubernetes_workload_allocation_daily (
              id INTEGER PRIMARY KEY AUTOINCREMENT, usage_date TEXT NOT NULL, vendor TEXT NOT NULL,
              account_id TEXT NOT NULL, cluster_name TEXT, cluster_location TEXT,
              allocation_scope TEXT NOT NULL, cost_component TEXT NOT NULL, namespace TEXT,
              workload_name TEXT, workload_type TEXT, author TEXT, org TEXT, repo TEXT,
              target_branch TEXT, allocation_weight REAL NOT NULL,
              source_node_list_cost REAL NOT NULL, list_cost REAL NOT NULL,
              allocation_method TEXT NOT NULL, allocation_version TEXT NOT NULL,
              dimension_hash TEXT NOT NULL, source_summary_row_hash TEXT,
              allocation_group_hash TEXT, calculated_at TEXT, updated_at TEXT,
              UNIQUE(usage_date, dimension_hash)
            )
            """,
            """
            CREATE TABLE cost_kubernetes_workload_allocation_source_daily (
              id INTEGER PRIMARY KEY AUTOINCREMENT, usage_date TEXT NOT NULL, vendor TEXT NOT NULL,
              account_id TEXT NOT NULL, source_summary_row_hash TEXT NOT NULL,
              allocation_group_hash TEXT NOT NULL, source_list_cost REAL NOT NULL,
              allocation_version TEXT NOT NULL, calculated_at TEXT, updated_at TEXT,
              UNIQUE(vendor, account_id, usage_date, source_summary_row_hash)
            )
            """,
        ):
            connection.execute(text(statement))
    return engine


def _rows() -> list[dict]:
    common = {
        "usage_date": "2026-08-10",
        "vendor": "gcp",
        "account_id": "project-1",
        "service_name": "Compute Engine",
        "sku_name": "N2 Core",
        "source_allocation_scope": "gke_direct",
        "cluster_name": "prow",
        "cluster_location": "us-central1-c",
        "kubernetes_cost_class": "direct",
        "kubernetes_residual_type": None,
        "kubernetes_cost_component": "cpu",
        "namespace": "prow",
        "workload_type": "core/v1-Pod",
        "org": "pingcap",
        "target_branch": "master",
        "effective_cost": "0",
        "credit_amount": "0",
        "net_cost": "0",
    }
    return [
        {**common, "workload_name": "pod-a", "author": "alice", "repo": "tidb", "list_cost": "75", "source_row_hash": "direct-a"},
        {**common, "workload_name": "pod-b", "author": "bob", "repo": "tikv", "list_cost": "25", "source_row_hash": "direct-b"},
        {
            **common,
            "source_allocation_scope": "gke_residual",
            "kubernetes_cost_class": "residual",
            "kubernetes_residual_type": "idle",
            "namespace": "kube:unallocated",
            "workload_name": None,
            "workload_type": None,
            "author": None,
            "repo": None,
            "list_cost": "100",
            "source_row_hash": "idle",
        },
        {
            **common,
            "source_allocation_scope": "gke_residual",
            "kubernetes_cost_class": "residual",
            "kubernetes_residual_type": "unsupported",
            "namespace": "goog-k8s-unsupported-sku",
            "workload_name": None,
            "workload_type": None,
            "author": None,
            "repo": None,
            "list_cost": "20",
            "source_row_hash": "unsupported",
        },
    ]


def test_native_gke_residual_uses_provider_direct_list_cost_and_retains_unsupported() -> None:
    allocations, sources = build_gke_workload_allocation_rows(
        account_id="project-1", summary_rows=_rows()
    )

    redistributed = [
        row
        for row in allocations
        if row["allocation_method"] == "gke_native_direct_list_cost"
    ]
    assert [(row["workload_name"], row["list_cost"]) for row in redistributed] == [
        ("pod-a", Decimal("75.00")),
        ("pod-b", Decimal("25.00")),
    ]
    assert sum(row["allocation_scope"] == "workload_split" for row in allocations) == 4
    assert [
        row["list_cost"] for row in allocations if row["allocation_scope"] == "unallocated"
    ] == [Decimal("20.00")]
    assert {row["source_summary_row_hash"] for row in sources} == {"idle"}
    assert all(row["allocation_version"] == ALLOCATION_VERSION for row in allocations)


def test_native_gke_allocation_preserves_subcent_cost() -> None:
    direct = {**_rows()[0], "list_cost": "0.001"}

    allocations, _ = build_gke_workload_allocation_rows(
        account_id="project-1",
        summary_rows=[direct],
    )

    assert allocations[0]["list_cost"] == Decimal("0.001")


def test_native_gke_pass_through_hash_preserves_distinct_summary_facts() -> None:
    direct = _rows()[0]
    allocations, _ = build_gke_workload_allocation_rows(
        account_id="project-1",
        summary_rows=[direct, {**direct, "source_row_hash": "direct-a-late"}],
    )

    assert len({row["dimension_hash"] for row in allocations}) == 2


def test_native_gke_sync_replaces_one_day_from_summary_without_metering() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (
                      usage_date, vendor, account_id, service_name, sku_name,
                      source_allocation_scope, cluster_name, cluster_location,
                      kubernetes_cost_class, kubernetes_residual_type,
                      kubernetes_cost_component, namespace, workload_name, workload_type,
                      author, org, repo, target_branch, list_cost, effective_cost,
                      credit_amount, net_cost, source_row_hash
                    ) VALUES (
                      :usage_date, :vendor, :account_id, :service_name, :sku_name,
                      :source_allocation_scope, :cluster_name, :cluster_location,
                      :kubernetes_cost_class, :kubernetes_residual_type,
                      :kubernetes_cost_component, :namespace, :workload_name, :workload_type,
                      :author, :org, :repo, :target_branch, :list_cost, :effective_cost,
                      :credit_amount, :net_cost, :source_row_hash
                    )
                    """
                ),
                _rows(),
            )
        result = run_sync_gcp_kubernetes_workload_allocations(
            engine,
            settings=GcpBillingSettings(account_id="project-1", page_size=2),
            usage_start_date=date(2026, 8, 10),
            usage_end_date=date(2026, 8, 10),
        )
        with engine.begin() as connection:
            total = connection.execute(
                text("SELECT SUM(list_cost) FROM cost_kubernetes_workload_allocation_daily")
            ).scalar_one()
        assert result.billing_rows_seen == 4
        assert result.rows_written == 5
        assert total == 220
    finally:
        engine.dispose()


def test_gke_day_replacement_rolls_back_on_write_failure(monkeypatch) -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_kubernetes_workload_allocation_daily (
                      usage_date, vendor, account_id, allocation_scope, cost_component,
                      allocation_weight, source_node_list_cost, list_cost,
                      allocation_method, allocation_version, dimension_hash
                    ) VALUES ('2026-08-10', 'gcp', 'project-1', 'workload_split', 'cpu',
                              1, 99, 99, 'old', 'v1', 'old-row')
                    """
                )
            )
        rows, source_rows = build_gke_workload_allocation_rows(
            account_id="project-1", summary_rows=[_rows()[0]]
        )

        def fail_write(*_args) -> None:
            raise RuntimeError("write failed")

        monkeypatch.setattr(gke_sync, "_write_rows", fail_write)
        with pytest.raises(RuntimeError, match="write failed"):
            gke_sync.replace_gke_workload_allocations(
                engine,
                rows,
                source_rows=source_rows,
                billing_row_count=1,
                account_id="project-1",
                usage_start_date=date(2026, 8, 10),
                usage_end_date=date(2026, 8, 10),
                dry_run=False,
                batch_size=1,
            )

        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT list_cost FROM cost_kubernetes_workload_allocation_daily")
            ).scalar_one() == 99
    finally:
        engine.dispose()


def test_gke_replacement_rejects_out_of_range_rows() -> None:
    engine = _sqlite_engine()
    try:
        rows, source_rows = build_gke_workload_allocation_rows(
            account_id="project-1", summary_rows=[_rows()[0]]
        )
        out_of_range = {**rows[0], "usage_date": date(2026, 8, 11)}

        with pytest.raises(ValueError, match="outside the replacement range"):
            gke_sync.replace_gke_workload_allocations(
                engine,
                (out_of_range,),
                source_rows=source_rows,
                billing_row_count=1,
                account_id="project-1",
                usage_start_date=date(2026, 8, 10),
                usage_end_date=date(2026, 8, 10),
                dry_run=False,
                batch_size=1,
            )
    finally:
        engine.dispose()


def test_cost_jobs_have_no_gke_metering_reference() -> None:
    source_root = Path(__file__).parents[1] / "src" / "cost_insight"
    source = "\n".join(
        path.read_text()
        for directory in (source_root / "jobs", source_root / "sources", source_root / "common")
        for path in directory.glob("*.py")
    )

    assert "gke_cluster_resource_usage" not in source
    assert "gke_usage_table" not in source


def test_gcp_summary_query_reads_native_cost_allocation_and_not_metering() -> None:
    query = build_gcp_billing_summary_query(billing_table="project.dataset.billing")

    assert "goog-k8s-cluster-name" in query
    assert "k8s-workload-name" in query
    assert "kube:unallocated" in query
    assert "goog-k8s-unsupported-sku" in query
    assert "gke_cluster_resource_usage" not in query
