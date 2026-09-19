import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from cost_insight.jobs import cli
from cost_insight.jobs.materialize_cost_allocations import run_materialize_cost_allocations
from cost_insight.jobs.refresh_attribution_daily import (
    CostAttributionSource,
    run_refresh_cost_attribution_from_summary,
)
from cost_insight.jobs.tencent_ci_allocation import (
    ACCOUNT_ID,
    beijing_usage_date,
    build_tencent_projection,
    build_weight,
    canonical_build_fingerprint,
    classify_tencent_row,
    complete_tencent_billing_partition,
    materialize_tencent_ci_cost_allocation,
    normalize_classification_rules,
    persist_tencent_billing_metadata,
    publish_tencent_ci_cost_allocation,
    publish_tencent_cost_classification,
    refresh_tencent_ci_build_staleness,
    resolve_direct_identity,
    resolve_identity,
)

DAY = date(2026, 9, 13)


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))
        connection.execute(
            text(
                """
                INSERT INTO roster_groups (id, lark_group_id, path, is_active, manager_id) VALUES (10, 'eq', '/10/', 1, 100)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO roster_employees
                  (id, email, github_id, en_name, group_id, manager_id, is_active)
                VALUES
                  (1, 'alice@example.com', 'alice', 'Alice', 10, 100, 1),
                  (2, 'alice-two@example.com', 'alice-two', 'Alice Two', 10, 100, 0)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tencent_cost_classification_rule_set
                  (classification_version, rules_json, content_hash, published_at)
                VALUES ('v1', :rules, 'rules-v1', '2026-09-01 00:00:00')
                """
            ),
            {
                "rules": json.dumps(
                    [
                        {
                            "business_code": "tke",
                            "product_code": "super",
                            "component_code": "cpu",
                            "item_code": "hour",
                            "classification": "supernode",
                        },
                        {
                            "business_code": "tke",
                            "product_code": "ordinary",
                            "component_code": "cpu",
                            "item_code": "hour",
                            "classification": "non_supernode",
                        },
                    ]
                )
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO tencent_ci_identity_alias_rule_set
                  (alias_version, aliases_json, content_hash, published_at)
                VALUES ('empty-v1', '{}', 'aliases-v1', '2026-09-01 00:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tencent_billing_import_partition
                  (account_id, bill_day, import_generation, is_complete, usage_dates_json)
                VALUES (:account_id, :day, 'generation-1', 1, :dates)
                """
            ),
            {"account_id": ACCOUNT_ID, "day": DAY, "dates": json.dumps([DAY.isoformat()])},
        )
        _insert_summary(connection, "super", "supernode", "10")
        _insert_summary(connection, "ordinary", "non_supernode", "90")
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_job_id, start_time, completion_time, cloud_phase, author, org, repo,
                  job_name, run_seconds, total_seconds
                ) VALUES
                  ('alice-build', '2026-09-12 16:30:00', '2026-09-12 17:30:00', 'TENCENT',
                   'alice', 'pingcap', 'tidb', 'unit', 3600, 0),
                  ('unknown-build', '2026-09-12 18:00:00', '2026-09-12 18:01:00', 'TENCENT',
                   'unknown', 'pingcap', 'tidb', 'unit', NULL, -1)
                """
            )
        )
    return engine


def _insert_summary(connection, product, classification, net_cost, *, usage_date=DAY):
    source_hash = f"{product}-hash"
    connection.execute(
        text(
            """
            INSERT INTO cost_bq_export_summary_daily (
              vendor, account_id, export_partition_date, usage_date, service_name, sku_name,
              usage_type, cost_driver_key, region, org, repo, target_branch, resource_name,
              vendor_tags_json, author, owner, service, project, service_exec_id, list_cost,
              effective_cost, credit_amount, net_cost, currency, source_row_hash,
              tencent_business_code, tencent_product_code, tencent_component_code, tencent_item_code,
              tencent_cost_class, tencent_classification_version, tencent_import_generation
            ) VALUES (
              'tencent', :account_id, :day, :day, 'TKE', :product, 'usage', 'compute', 'beijing',
              'raw-org', 'raw-repo', 'main', :product, '{"owner":"ignored"}', 'raw-author',
              'alice', 'raw-service', 'raw-project', 'raw-exec', :cost, :cost, NULL, :cost, 'CNY',
              :source_hash, 'tke', :product, 'cpu', 'hour', :classification, 'v1', 'generation-1'
            )
            """
        ),
        {
            "account_id": ACCOUNT_ID,
            "day": usage_date,
            "product": product,
            "cost": net_cost,
            "source_hash": source_hash,
            "classification": classification,
        },
    )


def test_beijing_day_and_rule_validation_are_explicit_and_fail_closed() -> None:
    assert beijing_usage_date(datetime(2026, 9, 12, 16, 0)) == DAY
    assert beijing_usage_date(datetime(2026, 9, 12, 16, tzinfo=UTC)) == DAY
    assert beijing_usage_date(None) is None
    with pytest.raises(ValueError, match="JSON list"):
        normalize_classification_rules({})
    with pytest.raises(ValueError, match="all four"):
        normalize_classification_rules([{"classification": "supernode"}])


def test_v1_weight_resolver_and_fail_closed_classification() -> None:
    roster = (
        {
            "employee_id": 1,
            "email": "alice@example.com",
            "github_id": "alice",
            "en_name": "Alice",
            "group_id": 10,
            "manager_id": 100,
        },
        {
            "employee_id": 2,
            "email": "same@example.com",
            "github_id": "same",
            "en_name": "Shared",
            "group_id": 20,
            "manager_id": 200,
        },
        {
            "employee_id": 3,
            "email": "same2@example.com",
            "github_id": "same2",
            "en_name": "Shared",
            "group_id": 30,
            "manager_id": 300,
        },
    )
    assert build_weight({"run_seconds": 3600, "total_seconds": 1}) == Decimal("2")
    assert build_weight({"run_seconds": 0, "total_seconds": -1}) == Decimal("1")
    assert resolve_identity("ALICE", roster=roster).employee_id == 1
    assert resolve_identity("same", roster=roster).employee_id == 2
    assert resolve_identity("Shared", roster=roster).matched is False
    assert resolve_identity(
        "duplicate",
        roster=(*roster, {**roster[1], "employee_id": 4, "github_id": "duplicate"}, {**roster[2], "github_id": "duplicate"}),
    ).matched is False
    assert resolve_identity("legacy-bot", roster=roster, aliases={"legacy-bot": 1}).path == "alias"
    assert resolve_direct_identity({"owner": "", "author": "alice"}, roster=roster, aliases={}).path == "author_github"
    assert resolve_direct_identity(
        {"owner": "unknown", "author": "alice"}, roster=roster, aliases={}
    ).path == "owner_unmatched"
    rules = (
        {
            "business_code": "tke",
            "product_code": "ordinary",
            "component_code": "cpu",
            "item_code": "hour",
            "classification": "non_supernode",
        },
    )
    assert classify_tencent_row(
        {
            "tencent_business_code": "tke",
            "tencent_product_code": "ordinary",
            "tencent_component_code": "cpu",
            "tencent_item_code": "hour",
        },
        rules,
    ) == "non_supernode"
    assert classify_tencent_row({}, rules) == "unclassified"


def test_pure_projection_conserves_weights_residual_and_direct_lineage() -> None:
    rows = (
        {
            "usage_date": DAY,
            "vendor": "tencent",
            "account_id": ACCOUNT_ID,
            "service_name": "TKE",
            "sku_name": "super",
            "cost_driver_key": "compute",
            "currency": "CNY",
            "source_row_hash": "direct",
            "owner": "alice",
            "author": "raw-author",
            "list_cost": Decimal("10"),
            "effective_cost": Decimal("10"),
            "credit_amount": None,
            "net_cost": Decimal("10"),
            "tencent_cost_class": "supernode",
        },
        {
            "usage_date": DAY,
            "vendor": "tencent",
            "account_id": ACCOUNT_ID,
            "service_name": "TKE",
            "cost_driver_key": "compute",
            "currency": "CNY",
            "source_row_hash": "shared",
            "list_cost": Decimal("90"),
            "effective_cost": Decimal("90"),
            "credit_amount": None,
            "net_cost": Decimal("90"),
            "tencent_cost_class": "non_supernode",
        },
    )
    roster = (
        {"employee_id": 1, "email": "alice@example.com", "github_id": "alice", "en_name": "Alice", "group_id": 10, "manager_id": 100},
    )
    builds = (
        {"author": "alice", "org": "pingcap", "repo": "tidb", "run_seconds": 3600},
        {"author": "unknown", "org": "pingcap", "repo": "tidb", "run_seconds": None, "total_seconds": -1},
    )
    projection, _, statistics = build_tencent_projection(
        ledger_rows=rows,
        builds=builds,
        roster=roster,
        aliases={},
        allocation_version="allocation-v1",
        classification_version="class-v1",
    )
    assert statistics["total_weight"] == "3"
    direct = next(row for row in projection if row["source_allocation_scope"] == "tencent_l3_direct")
    matched = next(row for row in projection if row["attribution_status"] == "matched" and row["source_pool_key"])
    residual = next(row for row in projection if row["attribution_status"] == "unattributed")
    assert direct["source_summary_row_hash"] == "direct"
    assert direct["source_pool_key"] is None
    assert matched["net_cost"] == Decimal("60.000000000")
    assert matched["allocation_weight"] == Decimal("2")
    assert residual["net_cost"] == Decimal("30.000000000")
    assert residual["owner"] is None
    assert sum(row["net_cost"] for row in projection) == Decimal("100")
    assert all(row["credit_amount"] is None for row in projection)


def test_unclassified_and_mixed_unknown_amounts_fail_closed() -> None:
    common = {
        "usage_date": DAY,
        "vendor": "tencent",
        "account_id": ACCOUNT_ID,
        "currency": "CNY",
        "source_row_hash": "row",
        "service_name": "TKE",
        "cost_driver_key": "compute",
        "list_cost": Decimal("1"),
        "effective_cost": Decimal("1"),
        "net_cost": Decimal("1"),
        "credit_amount": None,
    }
    with pytest.raises(ValueError, match="unclassified"):
        build_tencent_projection(
            ledger_rows=({**common, "tencent_cost_class": "unclassified"},),
            builds=(),
            roster=(),
            aliases={},
            allocation_version="v1",
            classification_version="v1",
        )
    with pytest.raises(ValueError, match="mixes known and unknown"):
        build_tencent_projection(
            ledger_rows=(
                {**common, "source_row_hash": "one", "tencent_cost_class": "non_supernode"},
                {**common, "source_row_hash": "two", "credit_amount": Decimal("1"), "tencent_cost_class": "non_supernode"},
            ),
            builds=(),
            roster=(),
            aliases={},
            allocation_version="v1",
            classification_version="v1",
        )


def test_materialization_rejects_an_incomplete_source_partition() -> None:
    engine = _engine()
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE tencent_billing_import_partition SET is_complete=0"))
        with pytest.raises(ValueError, match="incomplete source partition"):
            materialize_tencent_ci_cost_allocation(
                engine, start_date=DAY, end_date=DAY, allocation_version="blocked-v1"
            )
    finally:
        engine.dispose()


def test_rule_publish_is_append_only_and_marks_complete_ledger_dates_stale() -> None:
    engine = _engine()
    rules = [
        {
            "business_code": "tke",
            "product_code": "super",
            "component_code": "cpu",
            "item_code": "hour",
            "classification": "supernode",
        },
        {
            "business_code": "tke",
            "product_code": "ordinary",
            "component_code": "cpu",
            "item_code": "hour",
            "classification": "supernode",
        },
    ]
    try:
        assert publish_tencent_cost_classification(engine, classification_version="v2", rules=rules) == 1
        assert publish_tencent_cost_classification(engine, classification_version="v2", rules=rules) == 0
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT tencent_cost_class FROM cost_bq_export_summary_daily "
                    "WHERE source_row_hash='ordinary-hash'"
                )
            ).scalar_one() == "supernode"
            assert connection.execute(
                text("SELECT is_stale FROM tencent_ci_daily_state WHERE usage_date=:day"), {"day": DAY}
            ).scalar_one() == 1
        with pytest.raises(ValueError, match="immutable"):
            publish_tencent_cost_classification(engine, classification_version="v2", rules=[])
        assert publish_tencent_cost_classification(
            engine, classification_version="empty-v1", rules=[]
        ) == 1
        with pytest.raises(ValueError, match="content is already published as empty-v1"):
            publish_tencent_cost_classification(
                engine, classification_version="empty-v2", rules=[]
            )
    finally:
        engine.dispose()


def test_materialization_records_failed_day_and_keeps_prior_day() -> None:
    engine = _engine()
    failed_day = date(2026, 9, 14)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO tencent_billing_import_partition
                      (account_id, bill_day, import_generation, is_complete, usage_dates_json)
                    VALUES (:account_id, :day, 'generation-1', 1, :dates)
                    """
                ),
                {
                    "account_id": ACCOUNT_ID,
                    "day": failed_day,
                    "dates": json.dumps([failed_day.isoformat()]),
                },
            )
            _insert_summary(
                connection,
                "unclassified",
                "unclassified",
                "10",
                usage_date=failed_day,
            )
        with pytest.raises(ValueError, match="unclassified"):
            materialize_tencent_ci_cost_allocation(
                engine,
                start_date=DAY,
                end_date=failed_day,
                allocation_version="failed-range-v1",
            )
        with engine.connect() as connection:
            days = connection.execute(
                text(
                    """
                    SELECT usage_date, status, failure_reason
                    FROM tencent_ci_allocation_day
                    WHERE allocation_version='failed-range-v1'
                    ORDER BY usage_date
                    """
                )
            ).mappings().all()
        assert [(row["usage_date"], row["status"]) for row in days] == [
            (DAY.isoformat(), "validated"),
            (failed_day.isoformat(), "failed"),
        ]
        assert "unclassified" in days[1]["failure_reason"]
    finally:
        engine.dispose()


def test_materialize_publish_detector_and_rollback_are_atomic_and_idempotent() -> None:
    engine = _engine()
    try:
        result = materialize_tencent_ci_cost_allocation(
            engine, start_date=DAY, end_date=DAY, allocation_version="allocation-v1"
        )
        assert result.days_validated == 1
        assert result.projection_rows == 3
        published = publish_tencent_ci_cost_allocation(
            engine, allocation_version="allocation-v1", usage_dates=(DAY,)
        )
        assert published.published_days == 1
        assert publish_tencent_ci_cost_allocation(
            engine, allocation_version="allocation-v1", usage_dates=(DAY,)
        ).published_days == 0
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT SUM(net_cost) FROM cost_attribution_daily WHERE vendor='tencent'")
            ).scalar_one() == 100
            assert connection.execute(
                text("SELECT COUNT(*) FROM tencent_ci_allocation_publication_event")
            ).scalar_one() == 1

        with engine.begin() as connection:
            connection.execute(
                text("UPDATE ci_l1_builds SET run_seconds=7200 WHERE source_prow_job_id='alice-build'")
            )
        assert refresh_tencent_ci_build_staleness(engine) == (DAY,)
        with pytest.raises(ValueError, match="input changed"):
            publish_tencent_ci_cost_allocation(
                engine, allocation_version="allocation-v1", usage_dates=(DAY,)
            )

        materialize_tencent_ci_cost_allocation(
            engine, start_date=DAY, end_date=DAY, allocation_version="allocation-v2"
        )
        publish_tencent_ci_cost_allocation(engine, allocation_version="allocation-v2", usage_dates=(DAY,))
        rollback = publish_tencent_ci_cost_allocation(
            engine, allocation_version="allocation-v1", usage_dates=(DAY,), rollback=True
        )
        assert rollback.published_days == 1
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT active_allocation_version FROM tencent_ci_allocation_publication")
            ).scalar_one() == "allocation-v1"
            assert connection.execute(
                text("SELECT is_stale FROM tencent_ci_daily_state WHERE usage_date=:day"), {"day": DAY}
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT COUNT(*) FROM tencent_ci_allocation_publication_event")
            ).scalar_one() == 3
    finally:
        engine.dispose()


def test_import_metadata_replaces_completed_generation_and_keeps_stale_lineage() -> None:
    engine = _engine()
    try:
        with engine.begin() as connection:
            persist_tencent_billing_metadata(
                connection,
                account_id=ACCOUNT_ID,
                bill_day=DAY,
                import_generation="generation-2",
                rows=(
                    {
                        "source_row_hash": "ordinary-hash",
                        "tencent_business_code": "tke",
                        "tencent_product_code": "ordinary",
                        "tencent_component_code": "cpu",
                        "tencent_item_code": "hour",
                    },
                ),
            )
            complete_tencent_billing_partition(
                connection,
                account_id=ACCOUNT_ID,
                bill_day=DAY,
                import_generation="generation-2",
            )
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT is_complete FROM tencent_billing_import_partition")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT is_stale FROM tencent_ci_daily_state WHERE usage_date=:day"), {"day": DAY}
            ).scalar_one() == 1
        with engine.begin() as connection:
            connection.execute(text("UPDATE tencent_ci_daily_state SET is_stale=0"))
            persist_tencent_billing_metadata(
                connection,
                account_id=ACCOUNT_ID,
                bill_day=DAY,
                import_generation="generation-2",
                rows=(
                    {
                        "source_row_hash": "ordinary-hash",
                        "tencent_business_code": "tke",
                        "tencent_product_code": "ordinary",
                        "tencent_component_code": "cpu",
                        "tencent_item_code": "hour",
                    },
                ),
            )
            complete_tencent_billing_partition(
                connection,
                account_id=ACCOUNT_ID,
                bill_day=DAY,
                import_generation="generation-2",
            )
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT is_stale FROM tencent_ci_daily_state WHERE usage_date=:day"), {"day": DAY}
            ).scalar_one() == 0
    finally:
        engine.dispose()


def test_generic_refresh_and_derived_materialization_reject_or_skip_terminal_tencent() -> None:
    engine = _engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_sources (vendor, account_id, is_active, attribution_write_mode)
                    VALUES ('tencent', :account_id, 1, 'tencent_ci_published_terminal')
                    """
                ),
                {"account_id": ACCOUNT_ID},
            )
        with pytest.raises(ValueError, match="materialize-tencent-ci-cost-allocation"):
            run_refresh_cost_attribution_from_summary(
                engine,
                source=CostAttributionSource(vendor="tencent", account_id=ACCOUNT_ID),
                start_date=DAY,
                end_date=DAY,
            )
        result = run_materialize_cost_allocations(
            engine,
            start_date=DAY,
            end_date=DAY,
            earliest_date=DAY,
            eq_root_lark_group_id="eq",
            allocation_version="generic-v1",
            publish=False,
        )
        assert result.windows_seen == 0
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_allocation_daily")
            ).scalar_one() == 0
    finally:
        engine.dispose()


def test_cli_tencent_control_plane_and_publication_contracts(monkeypatch, tmp_path) -> None:
    captured = []

    class Engine:
        def dispose(self):
            captured.append("disposed")

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(
        cli,
        "publish_tencent_cost_classification",
        lambda _engine, **kwargs: captured.append(("classify", kwargs)) or 2,
    )
    monkeypatch.setattr(
        cli,
        "refresh_tencent_ci_build_staleness",
        lambda _engine: (DAY,),
    )
    monkeypatch.setattr(
        cli,
        "materialize_tencent_ci_cost_allocation",
        lambda _engine, **kwargs: SimpleNamespace(**kwargs, days_validated=1, projection_rows=3),
    )
    monkeypatch.setattr(
        cli,
        "publish_tencent_ci_cost_allocation",
        lambda _engine, **kwargs: SimpleNamespace(**kwargs, published_days=1),
    )
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("[]", encoding="utf-8")
    assert cli.main([
        "publish-tencent-cost-classification", "--classification-version", "v1",
        "--rules-file", str(rules_file), "--reviewed-by", "reviewer",
    ]) == 0
    assert captured[0] == (
        "classify",
        {"classification_version": "v1", "rules": [], "reviewed_by": "reviewer"},
    )
    assert cli.main(["refresh-tencent-ci-build-staleness"]) == 0
    assert cli.main([
        "materialize-tencent-ci-cost-allocation", "--start-date", DAY.isoformat(),
        "--end-date", DAY.isoformat(), "--allocation-version", "shadow-v1",
    ]) == 0
    assert cli.main([
        "publish-tencent-ci-cost-allocation", "--allocation-version", "shadow-v1",
        "--usage-date", DAY.isoformat(),
    ]) == 0
    assert cli.main([
        "rollback-tencent-ci-cost-allocation", "--allocation-version", "shadow-v1",
        "--usage-date", DAY.isoformat(),
    ]) == 0


def test_build_fingerprint_changes_for_all_contract_inputs() -> None:
    build = {
        "source_prow_job_id": "job",
        "start_time": "2026-09-12T16:00:00",
        "completion_time": "2026-09-12T17:00:00",
        "cloud_phase": "TENCENT",
        "author": "alice",
        "org": "pingcap",
        "repo": "tidb",
        "job_name": "unit",
        "run_seconds": 1,
        "total_seconds": 2,
        "participates": True,
    }
    assert canonical_build_fingerprint((build,)) != canonical_build_fingerprint(
        ({**build, "author": "bob"},)
    )


_SCHEMA = (
    """
    CREATE TABLE cost_sources (
      vendor TEXT, account_id TEXT, is_active INTEGER, attribution_write_mode TEXT,
      UNIQUE(vendor, account_id)
    )
    """,
    """
    CREATE TABLE roster_groups (id INTEGER PRIMARY KEY, lark_group_id TEXT, path TEXT, is_active INTEGER, manager_id INTEGER)
    """,
    """
    CREATE TABLE roster_employees (
      id INTEGER PRIMARY KEY, email TEXT, github_id TEXT, en_name TEXT, group_id INTEGER, manager_id INTEGER, is_active INTEGER
    )
    """,
    """
    CREATE TABLE ci_l1_builds (
      source_prow_job_id TEXT, start_time TEXT, completion_time TEXT, cloud_phase TEXT, author TEXT,
      org TEXT, repo TEXT, job_name TEXT, run_seconds NUMERIC, total_seconds NUMERIC
    )
    """,
    """
    CREATE TABLE cost_bq_export_summary_daily (
      vendor TEXT, account_id TEXT, export_partition_date TEXT, usage_date TEXT, service_name TEXT,
      sku_name TEXT, usage_type TEXT, cost_driver_key TEXT, region TEXT, org TEXT, repo TEXT,
      target_branch TEXT, resource_name TEXT, vendor_tags_json TEXT, author TEXT, owner TEXT,
      service TEXT, project TEXT, service_exec_id TEXT, list_cost NUMERIC, effective_cost NUMERIC,
      credit_amount NUMERIC, net_cost NUMERIC, currency TEXT, source_row_hash TEXT,
      tencent_business_code TEXT, tencent_product_code TEXT, tencent_component_code TEXT,
      tencent_item_code TEXT, tencent_cost_class TEXT, tencent_classification_version TEXT,
      tencent_import_generation TEXT
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
      source_summary_row_hash TEXT, allocation_version TEXT, classification_version TEXT, weight_model TEXT,
      weight_version TEXT, allocation_weight NUMERIC, source_pool_key TEXT, dimension_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_kubernetes_workload_allocation_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, cluster_location TEXT, allocation_scope TEXT,
      namespace TEXT, workload_name TEXT, workload_type TEXT, author TEXT, org TEXT, repo TEXT,
      target_branch TEXT, list_cost NUMERIC, allocation_weight NUMERIC, allocation_method TEXT,
      dimension_hash TEXT, source_summary_row_hash TEXT, allocation_group_hash TEXT
    )
    """,
    """
    CREATE TABLE cost_kubernetes_workload_allocation_source_daily (
      usage_date TEXT, vendor TEXT, account_id TEXT, source_summary_row_hash TEXT,
      allocation_group_hash TEXT, source_list_cost NUMERIC
    )
    """,
    """
    CREATE TABLE cost_allocation_daily (
      basis_key TEXT, allocation_version TEXT, allocation_stage TEXT, usage_date TEXT, vendor TEXT,
      account_id TEXT, service_name TEXT, sku_name TEXT, usage_type TEXT, cost_driver_key TEXT,
      region TEXT, org TEXT, repo TEXT, target_branch TEXT, resource_name TEXT, vendor_tags_json TEXT,
      source_allocation_scope TEXT, namespace TEXT, workload_name TEXT, workload_type TEXT, author TEXT,
      owner TEXT, service TEXT, project TEXT, service_exec_id TEXT, attribution_key TEXT,
      attribution_source TEXT, attribution_status TEXT, allocate_method TEXT, employee_id INTEGER,
      group_id INTEGER, manager_id INTEGER, usage_seconds NUMERIC, list_cost NUMERIC,
      effective_cost NUMERIC, credit_amount NUMERIC, net_cost NUMERIC, currency TEXT, source_rows INTEGER,
      source_summary_row_hash TEXT, source_fact_hash TEXT, source_owner TEXT, source_group_id INTEGER,
      source_manager_id INTEGER, target_group_id INTEGER, target_manager_id INTEGER, allocation_scope TEXT,
      allocation_method TEXT, allocation_weight NUMERIC, roster_resolved_at TEXT, dimension_hash TEXT
    )
    """,
    """
    CREATE TABLE tencent_cost_classification_rule_set (
      classification_version TEXT PRIMARY KEY, rules_json TEXT, content_hash TEXT, reviewed_by TEXT, published_at TEXT
    )
    """,
    """
    CREATE TABLE tencent_ci_identity_alias_rule_set (
      alias_version TEXT PRIMARY KEY, aliases_json TEXT, content_hash TEXT, reviewed_by TEXT, published_at TEXT
    )
    """,
    """
    CREATE TABLE tencent_billing_import_partition (
      account_id TEXT, bill_day TEXT, import_generation TEXT, is_complete INTEGER, usage_dates_json TEXT,
      source_fingerprint TEXT, completed_at TEXT, updated_at TEXT, PRIMARY KEY(account_id, bill_day)
    )
    """,
    """
    CREATE TABLE tencent_ci_daily_state (
      account_id TEXT, usage_date TEXT, ledger_fingerprint TEXT, build_fingerprint TEXT,
      active_allocation_version TEXT, is_stale INTEGER, stale_reason TEXT, updated_at TEXT,
      PRIMARY KEY(account_id, usage_date)
    )
    """,
    """
    CREATE TABLE tencent_ci_roster_snapshot (
      snapshot_id TEXT PRIMARY KEY, content_hash TEXT, roster_json TEXT, resolved_at TEXT
    )
    """,
    """
    CREATE TABLE tencent_ci_allocation_manifest (
      allocation_version TEXT PRIMARY KEY, account_id TEXT, start_date TEXT, end_date TEXT,
      classification_version TEXT, classification_hash TEXT, alias_version TEXT, alias_hash TEXT,
      roster_snapshot_id TEXT, weight_model TEXT, weight_version TEXT, algorithm_version TEXT, created_at TEXT
    )
    """,
    """
    CREATE TABLE tencent_ci_allocation_day (
      allocation_version TEXT, usage_date TEXT, status TEXT, ledger_fingerprint TEXT, build_fingerprint TEXT,
      pool_fingerprint TEXT, projection_row_count INTEGER, statistics_json TEXT, validated_at TEXT,
      published_at TEXT, failure_reason TEXT, PRIMARY KEY(allocation_version, usage_date)
    )
    """,
    """
    CREATE TABLE tencent_ci_allocation_projection (
      id INTEGER PRIMARY KEY AUTOINCREMENT, allocation_version TEXT, usage_date TEXT, vendor TEXT,
      account_id TEXT, service_name TEXT, sku_name TEXT, usage_type TEXT, cost_driver_key TEXT,
      region TEXT, org TEXT, repo TEXT, target_branch TEXT, resource_name TEXT, vendor_tags_json TEXT,
      source_allocation_scope TEXT, namespace TEXT, workload_name TEXT, workload_type TEXT, author TEXT,
      owner TEXT, service TEXT, project TEXT, service_exec_id TEXT, attribution_key TEXT,
      attribution_source TEXT, attribution_status TEXT, allocate_method TEXT, employee_id INTEGER,
      group_id INTEGER, manager_id INTEGER, usage_seconds NUMERIC, list_cost NUMERIC,
      effective_cost NUMERIC, credit_amount NUMERIC, net_cost NUMERIC, currency TEXT, source_rows INTEGER,
      source_summary_row_hash TEXT, classification_version TEXT, weight_model TEXT, weight_version TEXT,
      allocation_weight NUMERIC, source_pool_key TEXT, dimension_hash TEXT,
      UNIQUE(allocation_version, usage_date, dimension_hash)
    )
    """,
    """
    CREATE TABLE tencent_ci_allocation_publication (
      vendor TEXT, account_id TEXT, usage_date TEXT, active_allocation_version TEXT,
      ledger_fingerprint TEXT, build_fingerprint TEXT, published_at TEXT,
      PRIMARY KEY(vendor, account_id, usage_date)
    )
    """,
    """
    CREATE TABLE tencent_ci_allocation_publication_event (
      id INTEGER PRIMARY KEY AUTOINCREMENT, vendor TEXT, account_id TEXT, usage_date TEXT,
      allocation_version TEXT, event_type TEXT, previous_allocation_version TEXT, created_at TEXT
    )
    """,
)
