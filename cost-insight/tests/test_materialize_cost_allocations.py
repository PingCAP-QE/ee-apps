from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from cost_insight.jobs import materialize_cost_allocations
from cost_insight.jobs.materialize_cost_allocations import (
    build_eq_allocated_rows,
    build_kubernetes_allocated_rows,
    run_materialize_cost_allocations,
)


def _fact(*, group_id: int, list_cost: str, source_scope: str = "direct") -> dict:
    amount = Decimal(list_cost)
    return {
        "usage_date": date(2026, 8, 10),
        "vendor": "gcp",
        "account_id": "project-1",
        "source_allocation_scope": source_scope,
        "owner": f"owner-{group_id}",
        "employee_id": group_id,
        "group_id": group_id,
        "manager_id": group_id * 10,
        "list_cost": amount,
        "effective_cost": amount,
        "credit_amount": Decimal(),
        "net_cost": amount,
        "source_rows": 1,
        "dimension_hash": f"source-{group_id}-{list_cost}",
    }


def test_materialization_requires_the_configured_full_history_start() -> None:
    with pytest.raises(ValueError, match="configured allocation earliest date"):
        run_materialize_cost_allocations(
            create_engine("sqlite+pysqlite:///:memory:", future=True),
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            earliest_date=date(2026, 8, 1),
            eq_root_lark_group_id="eq",
        )


def test_eq_chargeback_uses_native_direct_list_cost_and_keeps_daily_account_boundary() -> None:
    native = (
        _fact(group_id=1, list_cost="30.00"),
        _fact(group_id=2, list_cost="75.00"),
        _fact(group_id=3, list_cost="25.00"),
    )
    # A prior K8s allocation changes the input cost but must not change the
    # native 75/25 EQ denominator.
    input_rows = (*native, _fact(group_id=2, list_cost="40.00", source_scope="gke_residual"))

    rows = build_eq_allocated_rows(
        input_rows=input_rows,
        native_rows=native,
        eq_group_ids={1},
        group_managers={2: 20, 3: 30},
        allocation_version="v1",
        roster_resolved_at=datetime(2026, 8, 21),
    )

    charged_eq = [row for row in rows if row["source_group_id"] == 1]
    assert [(row["group_id"], row["list_cost"]) for row in charged_eq] == [
        (2, Decimal("22.50")),
        (3, Decimal("7.50")),
    ]
    assert all(row["allocation_method"] == "eq_direct_list_cost" for row in charged_eq)
    assert sum((row["list_cost"] for row in rows), Decimal()) == Decimal("170.00")


def test_eq_chargeback_keeps_signed_cost_when_the_daily_account_has_no_denominator() -> None:
    source = {
        **_fact(group_id=1, list_cost="0.00"),
        "effective_cost": Decimal(),
        "credit_amount": Decimal("-10.00"),
        "net_cost": Decimal("-10.00"),
    }

    other_account = {**_fact(group_id=2, list_cost="100.00"), "account_id": "project-2"}
    rows = build_eq_allocated_rows(
        input_rows=(source,),
        native_rows=(source, other_account),
        eq_group_ids={1},
        group_managers={},
        allocation_version="v1",
        roster_resolved_at=datetime(2026, 8, 21),
    )

    assert len(rows) == 1
    assert rows[0]["group_id"] == 1
    assert rows[0]["net_cost"] == Decimal("-10.00")
    assert rows[0]["allocation_method"] == "eq_no_non_eq_direct_cost"


def test_kubernetes_basis_replaces_only_a_reconciled_residual_group() -> None:
    direct = _fact(group_id=2, list_cost="50.00")
    residual = {
        **_fact(group_id=1, list_cost="100.00", source_scope="gke_residual"),
        "source_summary_row_hash": "residual-source",
        "effective_cost": Decimal("80.00"),
        "credit_amount": Decimal("-10.00"),
        "net_cost": Decimal("70.00"),
    }
    allocation_rows = (
        {
            "usage_date": residual["usage_date"],
            "vendor": "gcp",
            "account_id": "project-1",
            "allocation_group_hash": "group-1",
            "allocation_scope": "workload_split",
            "namespace": "prow",
            "workload_name": "pod-a",
            "author": "alice",
            "repo": "repo-a",
            "list_cost": Decimal("50.00"),
            "allocation_weight": Decimal("0.50"),
            "allocation_method": "gke_native_direct_list_cost",
            "dimension_hash": "allocation-a",
        },
        {
            "usage_date": residual["usage_date"],
            "vendor": "gcp",
            "account_id": "project-1",
            "allocation_group_hash": "group-1",
            "allocation_scope": "workload_split",
            "namespace": "prow",
            "workload_name": "pod-a",
            "author": "alice",
            "repo": "repo-b",
            "list_cost": Decimal("25.00"),
            "allocation_weight": Decimal("0.25"),
            "allocation_method": "gke_native_direct_list_cost",
            "dimension_hash": "allocation-b",
        },
        {
            "usage_date": residual["usage_date"],
            "vendor": "gcp",
            "account_id": "project-1",
            "allocation_group_hash": "group-1",
            "allocation_scope": "workload_split",
            "namespace": "prow",
            "workload_name": "pod-b",
            "author": "bob",
            "list_cost": Decimal("25.00"),
            "allocation_weight": Decimal("0.25"),
            "allocation_method": "gke_native_direct_list_cost",
            "dimension_hash": "allocation-c",
        },
    )

    rows = build_kubernetes_allocated_rows(
        native_rows=(direct, residual),
        allocation_rows=allocation_rows,
        source_mappings=(
            {
                "usage_date": residual["usage_date"],
                "vendor": "gcp",
                "account_id": "project-1",
                "source_summary_row_hash": "residual-source",
                "allocation_group_hash": "group-1",
                "source_list_cost": Decimal("100.00"),
            },
        ),
        roster_by_identity={
            "alice": {"employee_id": 10, "group_id": 1, "manager_id": 10},
            "bob": {"employee_id": 20, "group_id": 2, "manager_id": 20},
        },
        allocation_version="v1",
        roster_resolved_at=datetime(2026, 8, 21),
    )

    allocated = [row for row in rows if row["allocation_stage"] == "kubernetes_residual"]
    assert [(row["group_id"], row["list_cost"], row["net_cost"]) for row in allocated] == [
        (1, Decimal("50.00"), Decimal("35.00")),
        (1, Decimal("25.00"), Decimal("17.50")),
        (2, Decimal("25.00"), Decimal("17.50")),
    ]
    assert len({row["dimension_hash"] for row in allocated}) == 3
    assert sum((row["net_cost"] for row in rows), Decimal()) == Decimal("120.00")
    assert {row["allocation_scope"] for row in allocated} == {"redistributed"}
    assert next(row for row in rows if row["source_fact_hash"] == direct["dimension_hash"])[
        "allocation_scope"
    ] == "direct"

    combined = build_eq_allocated_rows(
        input_rows=rows,
        native_rows=(direct, residual),
        eq_group_ids={1},
        group_managers={2: 20},
        allocation_version="v1",
        roster_resolved_at=datetime(2026, 8, 21),
        basis_key="kubernetes_eq_allocated",
    )
    assert {row["group_id"] for row in combined} == {2}
    assert {row["allocation_scope"] for row in combined} == {"direct", "redistributed"}
    for amount in ("list_cost", "effective_cost", "credit_amount", "net_cost"):
        assert sum((Decimal(row.get(amount) or 0) for row in combined), Decimal()) == sum(
            (Decimal(row.get(amount) or 0) for row in rows), Decimal()
        )

    unreconciled = build_kubernetes_allocated_rows(
        native_rows=(residual,),
        allocation_rows=(),
        source_mappings=(),
        roster_by_identity={},
        allocation_version="v1",
        roster_resolved_at=datetime(2026, 8, 21),
    )
    assert unreconciled[0]["allocation_scope"] == "residual_unallocated"


def test_materialize_job_publishes_all_three_daily_perspectives() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in _MATERIALIZE_SCHEMA:
            connection.execute(text(statement))
        connection.execute(
            text(
                """
                INSERT INTO roster_groups (id, lark_group_id, path, manager_id, is_active)
                VALUES (1, 'eq', '/1/', 10, 1), (2, 'database', '/2/', 20, 1),
                       (3, 'tikv', '/3/', 30, 1)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO roster_employees
                  (id, email, github_id, group_id, manager_id, is_active)
                VALUES (1, 'eq@example.com', 'eq', 1, 10, 1),
                       (2, 'db@example.com', 'db', 2, 20, 1),
                       (3, 'tikv@example.com', 'tikv', 3, 30, 1)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_attribution_daily (
                  usage_date, vendor, account_id, source_allocation_scope, owner,
                  attribution_source, attribution_status, employee_id, group_id,
                  manager_id, list_cost, effective_cost, credit_amount, net_cost,
                  source_rows, dimension_hash
                ) VALUES
                  ('2026-08-10', 'gcp', 'project-1', 'direct', 'eq@example.com',
                   'author', 'matched', 1, 1, 10, 30, 30, 0, 30, 1, 'eq-source'),
                  ('2026-08-10', 'gcp', 'project-1', 'direct', 'db@example.com',
                   'author', 'matched', 2, 2, 20, 75, 75, 0, 75, 1, 'db-source'),
                  ('2026-08-10', 'gcp', 'project-1', 'direct', 'tikv@example.com',
                   'author', 'matched', 3, 3, 30, 25, 25, 0, 25, 1, 'tikv-source')
                """
            )
        )

    result = run_materialize_cost_allocations(
        engine,
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        earliest_date=date(2026, 8, 10),
        eq_root_lark_group_id="eq",
        allocation_version="v1",
        now=datetime(2026, 8, 21),
    )

    with engine.begin() as connection:
        counts = connection.execute(
            text(
                """
                SELECT basis_key, COUNT(*), SUM(list_cost)
                FROM cost_allocation_daily GROUP BY basis_key ORDER BY basis_key
                """
            )
        ).all()
        active = connection.execute(
            text("SELECT active_allocation_version FROM cost_allocation_publication")
        ).scalar_one()
    assert result.rows_written == 11
    assert counts == [
        ("eq_allocated", 4, 130),
        ("kubernetes_allocated", 3, 130),
        ("kubernetes_eq_allocated", 4, 130),
    ]
    assert active == "v1"

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_attribution_daily (
                  usage_date, vendor, account_id, source_allocation_scope,
                  attribution_source, attribution_status, group_id, manager_id,
                  list_cost, effective_cost, credit_amount, net_cost, source_rows,
                  dimension_hash
                ) VALUES (
                  '2026-08-11', 'gcp', 'project-1', 'direct', 'author', 'matched',
                  2, 20, 1, 1, 0, 1, 1, 'future-source'
                )
                """
            )
        )
    with pytest.raises(ValueError, match="latest native cost date 2026-08-11"):
        run_materialize_cost_allocations(
            engine,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            earliest_date=date(2026, 8, 10),
            eq_root_lark_group_id="eq",
            allocation_version="partial",
            now=datetime(2026, 8, 21),
        )


def test_failed_conservation_does_not_replace_the_active_publication(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in _MATERIALIZE_SCHEMA:
            connection.execute(text(statement))
        connection.execute(
            text(
                """
                INSERT INTO roster_groups (id, lark_group_id, path, manager_id, is_active)
                VALUES (1, 'eq', '/1/', 10, 1)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_attribution_daily (
                  usage_date, vendor, account_id, source_allocation_scope,
                  attribution_source, attribution_status, group_id, manager_id,
                  list_cost, effective_cost, credit_amount, net_cost, source_rows,
                  dimension_hash
                ) VALUES (
                  '2026-08-10', 'gcp', 'project-1', 'direct', 'author', 'matched',
                  1, 10, 30, 30, 0, 30, 1, 'eq-source'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_allocation_publication
                  (publication_name, active_allocation_version)
                VALUES ('dashboard', 'old')
                """
            )
        )

    monkeypatch.setattr(materialize_cost_allocations, "build_eq_allocated_rows", lambda **_: ())
    with pytest.raises(RuntimeError, match="does not conserve"):
        run_materialize_cost_allocations(
            engine,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            earliest_date=date(2026, 8, 10),
            eq_root_lark_group_id="eq",
            allocation_version="broken",
            now=datetime(2026, 8, 21),
        )

    with engine.begin() as connection:
        active = connection.execute(
            text("SELECT active_allocation_version FROM cost_allocation_publication")
        ).scalar_one()
        staged = connection.execute(
            text("SELECT COUNT(*) FROM cost_allocation_daily WHERE allocation_version = 'broken'")
        ).scalar_one()
    assert active == "old"
    assert staged == 0


_MATERIALIZE_SCHEMA = (
    """
    CREATE TABLE roster_groups (
      id INTEGER PRIMARY KEY, lark_group_id TEXT, path TEXT, manager_id INTEGER, is_active INTEGER
    )
    """,
    """
    CREATE TABLE roster_employees (
      id INTEGER PRIMARY KEY, email TEXT, github_id TEXT, group_id INTEGER,
      manager_id INTEGER, is_active INTEGER
    )
    """,
    """
    CREATE TABLE cost_attribution_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, service_name TEXT, sku_name TEXT,
      usage_type TEXT, cost_driver_key TEXT, region TEXT, org TEXT, repo TEXT,
      target_branch TEXT, resource_name TEXT, vendor_tags_json TEXT,
      source_allocation_scope TEXT, namespace TEXT, workload_name TEXT, workload_type TEXT,
      author TEXT, owner TEXT, service TEXT, project TEXT, service_exec_id TEXT,
      attribution_key TEXT, attribution_source TEXT, attribution_status TEXT,
      allocate_method TEXT, employee_id INTEGER, group_id INTEGER, manager_id INTEGER,
      usage_seconds REAL, list_cost REAL, effective_cost REAL, credit_amount REAL,
      net_cost REAL, source_rows INTEGER, source_summary_row_hash TEXT, dimension_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_kubernetes_workload_allocation_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, cluster_location TEXT,
      allocation_scope TEXT, namespace TEXT, workload_name TEXT, workload_type TEXT,
      author TEXT, org TEXT, repo TEXT, target_branch TEXT, list_cost REAL,
      allocation_weight REAL, allocation_method TEXT, dimension_hash TEXT,
      source_summary_row_hash TEXT, allocation_group_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_kubernetes_workload_allocation_source_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, source_summary_row_hash TEXT,
      allocation_group_hash TEXT, source_list_cost REAL
    )
    """,
    """
    CREATE TABLE cost_allocation_daily (
      basis_key TEXT, allocation_version TEXT, allocation_stage TEXT, usage_date TEXT,
      vendor TEXT, account_id TEXT, service_name TEXT, sku_name TEXT, usage_type TEXT,
      cost_driver_key TEXT, region TEXT, org TEXT, repo TEXT, target_branch TEXT,
      resource_name TEXT, vendor_tags_json TEXT, source_allocation_scope TEXT,
      namespace TEXT, workload_name TEXT, workload_type TEXT, author TEXT, owner TEXT,
      service TEXT, project TEXT, service_exec_id TEXT, attribution_key TEXT,
      attribution_source TEXT, attribution_status TEXT, allocate_method TEXT,
      employee_id INTEGER, group_id INTEGER, manager_id INTEGER, usage_seconds REAL,
      list_cost REAL, effective_cost REAL, credit_amount REAL, net_cost REAL,
      source_rows INTEGER, source_summary_row_hash TEXT, source_fact_hash TEXT,
      source_owner TEXT, source_group_id INTEGER, source_manager_id INTEGER,
      target_group_id INTEGER, target_manager_id INTEGER, allocation_scope TEXT,
      allocation_method TEXT, allocation_weight REAL, roster_resolved_at TEXT,
      dimension_hash TEXT, UNIQUE(basis_key, allocation_version, usage_date, dimension_hash)
    )
    """,
    """
    CREATE TABLE cost_allocation_publication (
      publication_name TEXT PRIMARY KEY, active_allocation_version TEXT, updated_at TEXT
    )
    """,
)
