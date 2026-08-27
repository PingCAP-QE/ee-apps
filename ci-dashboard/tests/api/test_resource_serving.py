from datetime import date

from sqlalchemy import create_engine, event, text

from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.cost import get_unmatched_resources


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))
        connection.execute(
            text(
                """
                INSERT INTO cost_sources (vendor, account_id, is_active)
                VALUES ('gcp', 'project-1', 1)
                """
            )
        )
    return engine


def _filters():
    return CommonFilters(
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        granularity="week",
        cost_vendor="gcp",
        cost_account_id="project-1",
    )


def test_no_owner_resource_read_uses_only_published_serving_rows() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_resource_serving_publication (
                  basis_key, vendor, account_id, usage_date, active_materialization_version,
                  source_allocation_version, detail_list_cost, total_list_cost, source_row_count
                ) VALUES ('native', 'gcp', 'project-1', '2026-08-10', 'v1', NULL, 40, 100, 1)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_resource_serving_daily (
                  materialization_version, basis_key, usage_date, vendor, account_id, owner_key, owner,
                  target_branch, resource_group_key, resource_key, resource_name, service_name,
                  resource_identity_kind, representative_labels_json, metadata_variant_count,
                  detail_list_cost, fallback_list_cost, usage_seconds, list_cost, source_row_count
                ) VALUES
                  ('v1', 'native', '2026-08-10', 'gcp', 'project-1',
                   'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '',
                   'master', 'group-1', 'detail-1', 'instance-1', 'Compute Engine',
                   'resource_detail', '{"cluster":"prow"}', 1, 40, 0, 40, 40, 1),
                  ('v1', 'native', '2026-08-10', 'gcp', 'project-1',
                   'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '',
                   'master', 'group-2', 'fallback-1', '(resource detail unavailable)', 'Cloud Storage',
                   'attribution_fallback', NULL, 0, 0, 60, 0, 60, 1)
                """
            )
        )

    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda *_args: statements.append(_args[2]))
    result = get_unmatched_resources(engine, _filters())

    assert [item["resource_name"] for item in result["items"]] == [
        "(resource detail unavailable)",
        "instance-1",
    ]
    assert result["meta"]["resource_data_source"] == "mixed"
    assert result["meta"]["resource_detail_cost"] == 40
    assert result["meta"]["resource_detail_coverage_pct"] == 40.0
    executed = "\n".join(statements)
    assert "cost_resource_serving_daily" in executed
    assert "cost_unmatched_resource_daily" not in executed
    assert "cost_attribution_daily" not in executed


def test_stale_allocation_version_returns_pending_without_raw_fallback() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_allocation_publication (publication_name, active_allocation_version)
                VALUES ('dashboard', 'allocation-new')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_resource_serving_publication (
                  basis_key, vendor, account_id, usage_date, active_materialization_version,
                  source_allocation_version, detail_list_cost, total_list_cost, source_row_count
                ) VALUES ('kubernetes_allocated', 'gcp', 'project-1', '2026-08-10',
                  'v1', 'allocation-old', 100, 100, 1)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_resource_serving_daily (
                  materialization_version, basis_key, usage_date, vendor, account_id, owner_key, owner,
                  resource_group_key, resource_key, resource_name, resource_identity_kind,
                  detail_list_cost, fallback_list_cost, list_cost, source_row_count
                ) VALUES ('v1', 'kubernetes_allocated', '2026-08-10', 'gcp', 'project-1',
                  'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '',
                  'group-1', 'detail-1', 'instance-1', 'resource_detail', 100, 0, 100, 1)
                """
            )
        )

    result = get_unmatched_resources(
        engine, _filters(), allocation_basis="residual_allocated"
    )

    assert result["items"] == []
    assert result["meta"]["allocation_basis"] == "residual_allocated"
    assert result["meta"]["pending_dates"] == ["2026-08-10"]


def test_missing_publication_returns_pending_without_legacy_read() -> None:
    engine = _engine()
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda *_args: statements.append(_args[2]))

    result = get_unmatched_resources(engine, _filters())

    assert result["items"] == []
    assert result["meta"]["materialized"] is True
    assert result["meta"]["pending_dates"] == ["2026-08-10"]
    assert "cost_unmatched_resource_daily" not in "\n".join(statements)
    assert "cost_attribution_daily" not in "\n".join(statements)


_SCHEMA = (
    """
    CREATE TABLE cost_sources (
      vendor TEXT, account_id TEXT, is_active INTEGER, source_available_from TEXT
    )
    """,
    """
    CREATE TABLE cost_allocation_publication (
      publication_name TEXT PRIMARY KEY, active_allocation_version TEXT
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
      credit_amount REAL, net_cost REAL, source_row_count INTEGER
    )
    """,
    """
    CREATE TABLE cost_resource_serving_publication (
      basis_key TEXT, vendor TEXT, account_id TEXT, usage_date TEXT,
      active_materialization_version TEXT, source_allocation_version TEXT,
      detail_list_cost REAL, total_list_cost REAL, source_row_count INTEGER,
      published_at TEXT, tiflash_ready_at TEXT,
      PRIMARY KEY (basis_key, vendor, account_id, usage_date)
    )
    """,
)
