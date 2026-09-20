import hashlib
import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from cost_insight.jobs import allocate_tencent_ci_cost, cli
from cost_insight.jobs.allocate_tencent_ci_cost import (
    ACCOUNT_ID,
    run_allocate_tencent_ci_cost,
)
from cost_insight.jobs.materialize_cost_allocations import run_materialize_cost_allocations
from cost_insight.jobs.materialize_resource_serving import run_materialize_resource_serving
from cost_insight.jobs.refresh_attribution_daily import (
    CostAttributionSource,
    run_refresh_cost_attribution_from_summary,
)

DAY = date(2026, 9, 13)


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        _register_mysql_functions(connection)
        for statement in _SCHEMA:
            connection.execute(text(statement))
        connection.execute(
            text(
                """
                INSERT INTO roster_groups (id, lark_group_id, path, is_active, manager_id)
                VALUES (10, 'eq', '/10/', 1, 100), (20, 'ops', '/20/', 1, 200)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO roster_employees (id, email, github_id, en_name, group_id, manager_id, is_active)
                VALUES
                  (1, 'alice@example.com', 'alice', 'Alice', 10, 100, 1),
                  (2, 'bob@example.com', 'bob', 'Bob', 20, 200, 1),
                  (3, 'inactive@example.com', 'inactive', 'Inactive', 20, 200, 0)
                """
            )
        )
        _insert_summary(connection, "super", "sp_eks_supernode_intel_pod", "10", "8")
        _insert_summary(connection, "compute", "cvm", "90", "72")
        _insert_summary(connection, "storage", "cos", "10", "8")
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  start_time, completion_time, cloud_phase, author, org, repo, run_seconds, total_seconds
                ) VALUES
                  ('2026-09-12 16:10:00', '2026-09-12 18:10:00', 'TENCENT', 'alice', 'pingcap', 'repo-a', 3600, 7200),
                  ('2026-09-12 17:10:00', '2026-09-12 18:10:00', 'TENCENT', 'bob', 'pingcap', 'repo-b', 0, 3600),
                  ('2026-09-12 18:10:00', '2026-09-12 18:11:00', 'TENCENT', 'unknown', 'pingcap', 'repo-c', -1, 0),
                  ('2026-09-12 15:59:00', '2026-09-12 16:01:00', 'TENCENT', 'alice', 'pingcap', 'ignored', 3600, 3600),
                  ('2026-09-12 17:10:00', NULL, 'TENCENT', 'alice', 'pingcap', 'ignored', 3600, 3600),
                  ('2026-09-12 17:10:00', '2026-09-12 18:10:00', 'AWS', 'alice', 'pingcap', 'ignored', 3600, 3600)
                """
            )
        )
    return engine


def _insert_summary(connection, source_hash, product_code, list_cost, net_cost):
    connection.execute(
        text(
            """
            INSERT INTO cost_bq_export_summary_daily (
              usage_date, vendor, account_id, service_name, sku_name, usage_type, cost_driver_key, region,
              org, repo, target_branch, resource_name, vendor_tags_json, source_allocation_scope, author,
              owner, service, project, service_exec_id, list_cost, effective_cost, credit_amount, net_cost,
              currency, source_row_hash
            ) VALUES (
              :day, 'tencent', :account_id, 'TKE', :source_hash, 'usage', 'compute', 'beijing',
              'raw-org', 'raw-repo', 'main', :source_hash, :tags, 'direct', 'alice', 'alice',
              'raw-service', 'raw-project', 'raw-exec', :list_cost, :net_cost, NULL, :net_cost,
              'CNY', :source_hash
            )
            """
        ),
        {
            "day": DAY,
            "account_id": ACCOUNT_ID,
            "source_hash": source_hash,
            "tags": json.dumps({"__tencent_product_code": product_code}),
            "list_cost": list_cost,
            "net_cost": net_cost,
        },
    )


def test_allocation_keeps_supernode_direct_and_conserves_weighted_shared_cost(monkeypatch) -> None:
    engine = _engine()
    monkeypatch.setattr(allocate_tencent_ci_cost, "run_materialize_resource_serving", lambda *_args, **_kwargs: None)
    try:
        summary = run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        assert summary.days_processed == 1
        assert summary.rows_written == 3
        with engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT service_name, org, repo, owner, author, employee_id, group_id, usage_seconds,
                               list_cost, effective_cost, credit_amount, net_cost, source_rows,
                               source_summary_row_hash, attribution_status, allocate_method
                        FROM cost_attribution_daily ORDER BY employee_id IS NULL, employee_id
                        """
                    )
                ).mappings()
            ]
    finally:
        engine.dispose()

    direct = next(row for row in rows if row["source_summary_row_hash"] == "super")
    assert direct["service_name"] == "TKE"
    assert direct["owner"] == "alice@example.com"
    assert direct["net_cost"] == 8
    shared = [row for row in rows if row["source_summary_row_hash"] is None]
    assert [(row["employee_id"], row["org"], row["repo"], row["list_cost"], row["net_cost"]) for row in shared] == [
        (1, "pingcap", "repo-a", 50, 40),
        (2, "pingcap", "repo-b", 33.333333333, 26.666666667),
        (None, "pingcap", "repo-c", 16.666666667, 13.333333333),
    ]
    assert [row["usage_seconds"] for row in shared] == [7200, 3600, 0]
    assert all(row["credit_amount"] is None for row in shared)
    assert all(row["source_rows"] == 2 for row in shared)
    assert all(row["author"] is None for row in shared)
    assert all(row["allocate_method"] == "tencent_ci_build_weight_v1" for row in shared)
    assert sum(row["list_cost"] for row in shared) == 100
    assert sum(row["net_cost"] for row in shared) == 80


def test_shared_allocation_keeps_cloud_service_and_project_dimensions(monkeypatch) -> None:
    engine = _engine()
    monkeypatch.setattr(
        allocate_tencent_ci_cost,
        "run_materialize_resource_serving",
        lambda *_args, **_kwargs: None,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE cost_bq_export_summary_daily
                    SET service_name='CVM', resource_name='ins-1', service='cicd', project='cicd',
                        vendor_tags_json=:tags
                    WHERE source_row_hash='compute'
                    """
                ),
                {"tags": json.dumps({"__tencent_product_code": "cvm", "service": "cicd"})},
            )
            connection.execute(
                text(
                    """
                    UPDATE cost_bq_export_summary_daily
                    SET service_name='COS', resource_name='bucket-1', service='bazel', project='bazel',
                        vendor_tags_json=:tags
                    WHERE source_row_hash='storage'
                    """
                ),
                {"tags": json.dumps({"__tencent_product_code": "cos", "service": "bazel"})},
            )
        summary = run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT service_name, project, employee_id, list_cost, net_cost
                        FROM cost_attribution_daily
                        WHERE source_allocation_scope='tencent_ci_shared'
                        ORDER BY project, employee_id IS NULL, employee_id
                        """
                    )
                ).mappings()
            ]
    finally:
        engine.dispose()

    assert summary.rows_written == 6
    assert [(row["service_name"], row["project"]) for row in rows] == [
        ("COS", "bazel"),
        ("COS", "bazel"),
        ("COS", "bazel"),
        ("CVM", "cicd"),
        ("CVM", "cicd"),
        ("CVM", "cicd"),
    ]
    assert sum(Decimal(str(row["list_cost"])) for row in rows) == Decimal("100")
    assert sum(Decimal(str(row["net_cost"])) for row in rows) == Decimal("80")
    assert all(row["service_name"] != "Tencent CI shared" for row in rows)


def test_allocation_publishes_tencent_resource_details() -> None:
    engine = _engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE cost_bq_export_summary_daily
                    SET service_name='CVM', resource_name='ins-1', service='cicd', project='cicd',
                        vendor_tags_json=:tags
                    WHERE source_row_hash='compute'
                    """
                ),
                {"tags": json.dumps({"__tencent_product_code": "cvm", "service": "cicd"})},
            )
            connection.execute(
                text(
                    """
                    UPDATE cost_bq_export_summary_daily
                    SET service_name='COS', resource_name='bucket-1', service='bazel', project='bazel',
                        vendor_tags_json=:tags, list_cost=0
                    WHERE source_row_hash='storage'
                    """
                ),
                {"tags": json.dumps({"__tencent_product_code": "cos", "service": "bazel"})},
            )
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT s.resource_id, s.resource_name, s.service_name, s.project, s.owner,
                               s.group_id, s.representative_labels_json, s.list_cost, s.net_cost,
                               s.resource_identity_kind
                        FROM cost_resource_serving_daily s
                        JOIN cost_resource_serving_publication p
                          ON p.basis_key=s.basis_key AND p.vendor=s.vendor
                         AND p.account_id=s.account_id AND p.usage_date=s.usage_date
                         AND p.active_materialization_version=s.materialization_version
                        ORDER BY s.resource_id, s.owner
                        """
                    )
                ).mappings()
            ]
    finally:
        engine.dispose()

    assert {row["resource_id"] for row in rows} == {"super", "ins-1", "bucket-1"}
    assert {(row["resource_id"], row["service_name"], row["project"]) for row in rows} == {
        ("super", "TKE", "raw-project"),
        ("ins-1", "CVM", "cicd"),
        ("bucket-1", "COS", "bazel"),
    }
    assert all(row["resource_name"] == row["resource_id"] for row in rows)
    assert all(row["resource_identity_kind"] == "resource_detail" for row in rows)
    assert all(row["representative_labels_json"] for row in rows)
    assert sum(Decimal(str(row["list_cost"])) for row in rows) == Decimal("100")
    assert sum(Decimal(str(row["net_cost"])) for row in rows) == Decimal("88")
    assert sum(
        Decimal(str(row["net_cost"])) for row in rows if row["resource_id"] == "bucket-1"
    ) == Decimal("8")
    assert {row["owner"] for row in rows if row["resource_id"] == "ins-1"} == {
        "",
        "alice@example.com",
        "bob@example.com",
    }


def test_resource_serving_fails_clearly_when_direct_tencent_summary_is_reimported(
    monkeypatch,
) -> None:
    engine = _engine()
    monkeypatch.setattr(
        allocate_tencent_ci_cost,
        "run_materialize_resource_serving",
        lambda *_args, **_kwargs: None,
    )
    try:
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM cost_bq_export_summary_daily WHERE source_row_hash='super'")
            )

        with pytest.raises(RuntimeError, match="Tencent source-summary hash is missing: super"):
            run_materialize_resource_serving(engine, start_date=DAY, end_date=DAY)
    finally:
        engine.dispose()


def test_resource_serving_keeps_explicit_project_service_pools_separate() -> None:
    engine = _engine()
    try:
        with engine.begin() as connection:
            for source_hash, resource_name, service, tags in (
                (
                    "compute",
                    "cache-a",
                    "bazel",
                    {"__tencent_product_code": "cvm", "service": "bazel", "project": "cache"},
                ),
                (
                    "storage",
                    "cache-b",
                    "tikv",
                    {"__tencent_product_code": "cos", "service": "tikv", "project": "cache"},
                ),
            ):
                connection.execute(
                    text(
                        """
                        UPDATE cost_bq_export_summary_daily
                        SET service_name='COS', resource_name=:resource_name, service=:service,
                            project='cache', vendor_tags_json=:tags
                        WHERE source_row_hash=:source_hash
                        """
                    ),
                    {
                        "source_hash": source_hash,
                        "resource_name": resource_name,
                        "service": service,
                        "tags": json.dumps(tags),
                    },
                )
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT resource_id, owner, list_cost, net_cost, representative_labels_json
                        FROM cost_resource_serving_daily
                        WHERE resource_id IN ('cache-a', 'cache-b')
                        """
                    )
                ).mappings()
            ]
    finally:
        engine.dispose()

    amounts = {
        (row["resource_id"], row["owner"]): (
            Decimal(str(row["list_cost"])),
            Decimal(str(row["net_cost"])),
        )
        for row in rows
    }
    assert amounts == {
        ("cache-a", "alice@example.com"): (Decimal("45"), Decimal("36")),
        ("cache-a", "bob@example.com"): (Decimal("30"), Decimal("24")),
        ("cache-a", ""): (Decimal("15"), Decimal("12")),
        ("cache-b", "alice@example.com"): (Decimal("5"), Decimal("4")),
        ("cache-b", "bob@example.com"): (Decimal("3.333333333"), Decimal("2.666666667")),
        ("cache-b", ""): (Decimal("1.666666667"), Decimal("1.333333333")),
    }
    assert {json.loads(row["representative_labels_json"])["service"] for row in rows} == {
        "bazel",
        "tikv",
    }


def test_resource_serving_uses_fallback_identity_when_tencent_resource_id_is_missing() -> None:
    engine = _engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE cost_bq_export_summary_daily
                    SET resource_name=NULL
                    WHERE source_row_hash IN ('compute', 'storage')
                    """
                )
            )
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT resource_id, resource_name, resource_identity_kind, list_cost, net_cost
                        FROM cost_resource_serving_daily
                        WHERE resource_identity_kind='attribution_fallback'
                        """
                    )
                ).mappings()
            ]
    finally:
        engine.dispose()

    assert len(rows) == 6
    assert {(row["resource_id"], row["resource_name"]) for row in rows} == {
        (None, "(resource detail unavailable)")
    }
    assert sum(Decimal(str(row["list_cost"])) for row in rows) == Decimal("100")
    assert sum(Decimal(str(row["net_cost"])) for row in rows) == Decimal("80")


@pytest.mark.parametrize(
    ("vendor_tags_json", "error"),
    (("{}", "reimport first"), ("not-json", "invalid vendor tags")),
)
def test_invalid_product_metadata_fails_before_the_day_is_replaced(
    monkeypatch, vendor_tags_json, error
) -> None:
    engine = _engine()
    monkeypatch.setattr(allocate_tencent_ci_cost, "run_materialize_resource_serving", lambda *_args, **_kwargs: None)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_attribution_daily (
                      usage_date, vendor, account_id, service_name, attribution_source, attribution_status,
                      source_rows, dimension_hash
                    ) VALUES (:day, 'tencent', :account_id, 'before', 'test', 'matched', 1, 'before')
                    """
                ),
                {"day": DAY, "account_id": ACCOUNT_ID},
            )
            connection.execute(
                text(
                    "UPDATE cost_bq_export_summary_daily SET vendor_tags_json=:vendor_tags_json "
                    "WHERE source_row_hash='compute'"
                ),
                {"vendor_tags_json": vendor_tags_json},
            )
        with pytest.raises(ValueError, match=error):
            run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT service_name FROM cost_attribution_daily WHERE dimension_hash='before'")
            ).scalar_one() == "before"
    finally:
        engine.dispose()


def test_build_groups_preserve_repo_attribution_and_conserve_deterministically(monkeypatch) -> None:
    engine = _engine()
    monkeypatch.setattr(allocate_tencent_ci_cost, "run_materialize_resource_serving", lambda *_args, **_kwargs: None)
    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM ci_l1_builds"))
            connection.execute(
                text(
                    """
                    INSERT INTO ci_l1_builds (
                      start_time, completion_time, cloud_phase, author, org, repo, run_seconds, total_seconds
                    ) VALUES
                      ('2026-09-12 16:10:00', '2026-09-12 18:10:00', 'TENCENT', 'alice', 'pingcap', 'repo-a', 0, 0),
                      ('2026-09-12 16:10:00', '2026-09-12 18:10:00', 'TENCENT', 'alice', 'pingcap', 'repo-b', 0, 0),
                      ('2026-09-12 16:10:00', '2026-09-12 18:10:00', 'TENCENT', 'bob', 'pingcap', 'repo-a', 0, 0),
                      ('2026-09-12 16:10:00', '2026-09-12 18:10:00', 'TENCENT', 'unknown', 'pingcap', 'unmatched-a', 0, 0),
                      ('2026-09-12 16:10:00', '2026-09-12 18:10:00', 'TENCENT', 'unknown', 'pingcap', 'unmatched-b', 0, 0),
                      ('2026-09-12 16:10:00', '2026-09-12 18:10:00', 'TENCENT', 'unknown', 'pingcap', NULL, 0, 0)
                    """
                )
            )
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            shared = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT dimension_hash, employee_id, org, repo, owner, group_id, manager_id, list_cost, net_cost
                        FROM cost_attribution_daily
                        WHERE source_allocation_scope='tencent_ci_shared'
                        ORDER BY employee_id IS NULL, employee_id, org, repo
                        """
                    )
                ).mappings()
            ]
    finally:
        engine.dispose()

    assert [(row["employee_id"], row["org"], row["repo"]) for row in shared] == [
        (1, "pingcap", "repo-a"),
        (1, "pingcap", "repo-b"),
        (2, "pingcap", "repo-a"),
        (None, "pingcap", None),
        (None, "pingcap", "unmatched-a"),
        (None, "pingcap", "unmatched-b"),
    ]
    assert len({row["dimension_hash"] for row in shared}) == len(shared)
    assert all(
        row["owner"] is row["group_id"] is row["manager_id"] is None
        for row in shared
        if row["employee_id"] is None
    )
    assert shared[-1]["list_cost"] == 16.666666665
    assert sum(Decimal(str(row["list_cost"])) for row in shared) == Decimal("100")
    assert sum(Decimal(str(row["net_cost"])) for row in shared) == Decimal("80")


def test_all_matched_builds_conserve_the_rounding_remainder(monkeypatch) -> None:
    engine = _engine()
    monkeypatch.setattr(allocate_tencent_ci_cost, "run_materialize_resource_serving", lambda *_args, **_kwargs: None)
    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM ci_l1_builds WHERE author='unknown'"))
            connection.execute(
                text("UPDATE ci_l1_builds SET run_seconds=1, total_seconds=1 WHERE author='bob'")
            )
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            shared = list(
                connection.execute(
                    text(
                        "SELECT employee_id, list_cost, net_cost FROM cost_attribution_daily "
                        "WHERE source_allocation_scope='tencent_ci_shared' ORDER BY employee_id"
                    )
                ).mappings()
            )
    finally:
        engine.dispose()

    assert [row["employee_id"] for row in shared] == [1, 2]
    assert sum(Decimal(str(row["list_cost"])) for row in shared) == Decimal("100")
    assert sum(Decimal(str(row["net_cost"])) for row in shared) == Decimal("80")


def test_no_builds_use_one_residual_and_preserve_null_amounts(monkeypatch) -> None:
    engine = _engine()
    monkeypatch.setattr(allocate_tencent_ci_cost, "run_materialize_resource_serving", lambda *_args, **_kwargs: None)
    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM ci_l1_builds"))
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            shared = connection.execute(
                text(
                    """
                    SELECT org, repo, owner, employee_id, usage_seconds, list_cost, effective_cost, credit_amount, net_cost
                    FROM cost_attribution_daily WHERE source_allocation_scope='tencent_ci_shared'
                    """
                )
            ).mappings().one()
    finally:
        engine.dispose()

    assert dict(shared) == {
        "org": None,
        "repo": None,
        "owner": None,
        "employee_id": None,
        "usage_seconds": 0,
        "list_cost": 100,
        "effective_cost": 80,
        "credit_amount": None,
        "net_cost": 80,
    }


def test_rerun_replaces_the_day_and_failure_rolls_back_the_day(monkeypatch) -> None:
    engine = _engine()
    monkeypatch.setattr(allocate_tencent_ci_cost, "run_materialize_resource_serving", lambda *_args, **_kwargs: None)
    try:
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.begin() as connection:
            connection.execute(text("UPDATE cost_bq_export_summary_daily SET net_cost=90 WHERE source_row_hash='compute'"))
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT SUM(net_cost) FROM cost_attribution_daily WHERE source_allocation_scope='tencent_ci_shared'")
            ).scalar_one() == 98
            before_rerun = connection.execute(
                text(
                    """
                    SELECT dimension_hash, org, repo, employee_id, list_cost, effective_cost, credit_amount, net_cost
                    FROM cost_attribution_daily ORDER BY dimension_hash
                    """
                )
            ).all()
        run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    """
                    SELECT dimension_hash, org, repo, employee_id, list_cost, effective_cost, credit_amount, net_cost
                    FROM cost_attribution_daily ORDER BY dimension_hash
                    """
                )
            ).all() == before_rerun
            before_failure = connection.execute(
                text("SELECT COUNT(*) FROM cost_attribution_daily")
            ).scalar_one()
        monkeypatch.setattr(allocate_tencent_ci_cost, "_INSERT_SHARED_ROW", text("INSERT INTO missing VALUES (1)"))
        with pytest.raises(Exception):
            run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM cost_attribution_daily")).scalar_one() == before_failure
    finally:
        engine.dispose()


def test_dry_run_and_generic_guards_do_not_write_tencent_projection(monkeypatch) -> None:
    engine = _engine()
    monkeypatch.setattr(allocate_tencent_ci_cost, "run_materialize_resource_serving", lambda *_args, **_kwargs: None)
    try:
        dry_run = run_allocate_tencent_ci_cost(engine, start_date=DAY, end_date=DAY, dry_run=True)
        assert dry_run.rows_written == 3
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM cost_attribution_daily")).scalar_one() == 0
        with pytest.raises(ValueError, match="allocate-tencent-ci-cost"):
            run_refresh_cost_attribution_from_summary(
                engine,
                source=CostAttributionSource(vendor="tencent", account_id=ACCOUNT_ID),
                start_date=DAY,
                end_date=DAY,
            )
        with engine.begin() as connection:
            for usage_date in (DAY, date(2026, 9, 14)):
                connection.execute(
                    text(
                        """
                        INSERT INTO cost_attribution_daily (
                          usage_date, vendor, account_id, attribution_source, attribution_status,
                          source_rows, dimension_hash
                        ) VALUES (:usage_date, 'tencent', :account_id, 'test', 'matched', 1, :dimension_hash)
                        """
                    ),
                    {
                        "usage_date": usage_date,
                        "account_id": ACCOUNT_ID,
                        "dimension_hash": f"guard-{usage_date}",
                    },
                )
        result = run_materialize_cost_allocations(
            engine,
            start_date=DAY,
            end_date=DAY,
            earliest_date=DAY,
            eq_root_lark_group_id="eq",
            allocation_version="test",
            publish=False,
        )
        assert result.windows_seen == 0
    finally:
        engine.dispose()


def test_generic_refresh_discovery_skips_tencent_ci_source(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_list_sources",
        lambda *_args, **_kwargs: (
            SimpleNamespace(vendor="gcp", account_id="project"),
            SimpleNamespace(vendor="tencent", account_id=ACCOUNT_ID),
        ),
    )

    assert cli._resolve_attribution_sources(
        object(),
        gcp_settings=SimpleNamespace(account_id="fallback"),
        aws_settings=SimpleNamespace(account_id=None),
    ) == (cli.CostAttributionSource(vendor="gcp", account_id="project"),)


def test_cli_allocates_tencent_cost(monkeypatch) -> None:
    captured = []

    class Engine:
        def dispose(self):
            captured.append("disposed")

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(
        cli,
        "run_allocate_tencent_ci_cost",
        lambda _engine, **kwargs: captured.append(kwargs) or SimpleNamespace(**kwargs, days_processed=1, rows_written=2),
    )

    assert cli.main([
        "allocate-tencent-ci-cost", "--start-date", DAY.isoformat(), "--end-date", DAY.isoformat(), "--dry-run"
    ]) == 0
    assert captured[0] == {"start_date": DAY, "end_date": DAY, "dry_run": True}


def _register_mysql_functions(connection) -> None:
    raw = connection.connection.driver_connection
    raw.create_function("CONCAT", -1, lambda *values: "".join("" if value is None else str(value) for value in values))
    raw.create_function("CONCAT_WS", -1, lambda separator, *values: str(separator).join(str(value) for value in values if value is not None))
    raw.create_function("DATE_FORMAT", 2, lambda value, _format: str(value)[:10] if value is not None else None)
    raw.create_function("SHA2", 2, lambda value, _bits: hashlib.sha256(str(value or "").encode()).hexdigest())
    raw.create_function(
        "SUBSTRING_INDEX",
        3,
        lambda value, delimiter, count: str(delimiter).join(str(value or "").split(str(delimiter))[: int(count)]),
    )


_SCHEMA = (
    """
    CREATE TABLE roster_groups (
      id INTEGER PRIMARY KEY, lark_group_id TEXT, path TEXT, is_active INTEGER, manager_id INTEGER
    )
    """,
    """
    CREATE TABLE roster_employees (
      id INTEGER PRIMARY KEY, email TEXT, github_id TEXT, en_name TEXT, group_id INTEGER,
      manager_id INTEGER, is_active INTEGER
    )
    """,
    """
    CREATE TABLE ci_l1_builds (
      start_time TEXT, completion_time TEXT, cloud_phase TEXT, author TEXT, org TEXT, repo TEXT,
      run_seconds NUMERIC, total_seconds NUMERIC
    )
    """,
    """
    CREATE TABLE cost_kubernetes_pvc_pod_mapping (
      vendor TEXT, account_id TEXT, persistent_volume_name TEXT, pod_uid TEXT, author TEXT, org TEXT, repo TEXT
    )
    """,
    """
    CREATE TABLE cost_bq_export_summary_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, service_name TEXT, sku_name TEXT, usage_type TEXT,
      cost_driver_key TEXT, region TEXT, org TEXT, repo TEXT, target_branch TEXT, resource_name TEXT,
      vendor_tags_json TEXT, source_allocation_scope TEXT, namespace TEXT, workload_name TEXT,
      workload_type TEXT, author TEXT, owner TEXT, service TEXT, project TEXT, service_exec_id TEXT,
      list_cost NUMERIC, effective_cost NUMERIC, credit_amount NUMERIC, net_cost NUMERIC, currency TEXT,
      source_row_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_attribution_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, service_name TEXT, sku_name TEXT, usage_type TEXT,
      cost_driver_key TEXT, region TEXT, org TEXT, repo TEXT, target_branch TEXT, resource_name TEXT,
      vendor_tags_json TEXT, source_allocation_scope TEXT, namespace TEXT, workload_name TEXT,
      workload_type TEXT, author TEXT, owner TEXT, service TEXT, project TEXT, service_exec_id TEXT,
      attribution_key TEXT, attribution_source TEXT, attribution_status TEXT, allocate_method TEXT,
      employee_id INTEGER, group_id INTEGER, manager_id INTEGER, usage_seconds NUMERIC, list_cost NUMERIC,
      effective_cost NUMERIC, credit_amount NUMERIC, net_cost NUMERIC, currency TEXT, source_rows INTEGER,
      dimension_hash TEXT, source_summary_row_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_unmatched_resource_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, source_summary_row_hash TEXT,
      resource_name TEXT, resource_id TEXT, parent_resource_name TEXT, service_name TEXT,
      vendor_tags_json TEXT, usage_seconds NUMERIC, list_cost NUMERIC,
      currency TEXT NOT NULL DEFAULT 'USD', source_row_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_resource_serving_daily (
      id INTEGER PRIMARY KEY AUTOINCREMENT, materialization_version TEXT, basis_key TEXT,
      usage_date TEXT, vendor TEXT, account_id TEXT, owner_key TEXT, owner TEXT,
      group_id INTEGER, manager_id INTEGER, project TEXT, target_branch TEXT,
      resource_group_key TEXT, resource_key TEXT, resource_name TEXT, resource_id TEXT,
      service_name TEXT, resource_identity_kind TEXT, representative_labels_json TEXT,
      metadata_variant_count INTEGER, detail_list_cost NUMERIC, fallback_list_cost NUMERIC,
      usage_seconds NUMERIC, list_cost NUMERIC, effective_cost NUMERIC, credit_amount NUMERIC,
      net_cost NUMERIC, currency TEXT NOT NULL DEFAULT 'USD', source_row_count INTEGER,
      calculated_at TEXT,
      UNIQUE (materialization_version, basis_key, vendor, account_id, usage_date,
              owner_key, resource_key, target_branch)
    )
    """,
    """
    CREATE TABLE cost_resource_serving_publication (
      basis_key TEXT, vendor TEXT, account_id TEXT, usage_date TEXT,
      active_materialization_version TEXT, source_allocation_version TEXT,
      detail_list_cost NUMERIC, total_list_cost NUMERIC,
      currency TEXT NOT NULL DEFAULT 'USD', source_row_count INTEGER,
      published_at TEXT DEFAULT CURRENT_TIMESTAMP, tiflash_ready_at TEXT,
      PRIMARY KEY (basis_key, vendor, account_id, usage_date)
    )
    """,
)
