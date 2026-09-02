from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text

from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.cost import (
    _cost_billing_summary_table_exists,
    _decode_resource_cursor,
    _encode_resource_cursor,
    _format_vendor_labels,
    get_unmatched_resources,
)


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


def _filters(*, end_date: date = date(2026, 8, 10)):
    return CommonFilters(
        start_date=date(2026, 8, 10),
        end_date=end_date,
        granularity="week",
        cost_vendor="gcp",
        cost_account_id="project-1",
    )


def _publish(connection, usage_date: str, version: str = "v1") -> None:
    connection.execute(
        text(
            """
            INSERT INTO cost_resource_serving_publication (
              basis_key, vendor, account_id, usage_date, active_materialization_version,
              source_allocation_version, detail_list_cost, total_list_cost, source_row_count
            ) VALUES ('native', 'gcp', 'project-1', :usage_date, :version, NULL, 40, 100, 1)
            """
        ),
        {"usage_date": usage_date, "version": version},
    )


def _serving_row(
    connection,
    *,
    usage_date: str,
    resource_group_key: str,
    resource_key: str,
    resource_name: str,
    resource_id: str | None,
    service_name: str,
    labels: str | None,
    usage_seconds: float | None,
    list_cost: float,
    detail_list_cost: float | None = None,
    fallback_list_cost: float = 0,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO cost_resource_serving_daily (
              materialization_version, basis_key, usage_date, vendor, account_id, owner_key, owner,
              target_branch, resource_group_key, resource_key, resource_name, resource_id, service_name,
              resource_identity_kind, representative_labels_json, metadata_variant_count,
              detail_list_cost, fallback_list_cost, usage_seconds, list_cost, source_row_count
            ) VALUES (
              'v1', 'native', :usage_date, 'gcp', 'project-1',
              'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', '',
              'master', :resource_group_key, :resource_key, :resource_name, :resource_id, :service_name,
              'resource_detail', :labels, 1, :detail_list_cost, :fallback_list_cost,
              :usage_seconds, :list_cost, 1
            )
            """
        ),
        {
            "usage_date": usage_date,
            "resource_group_key": resource_group_key,
            "resource_key": resource_key,
            "resource_name": resource_name,
            "resource_id": resource_id,
            "service_name": service_name,
            "labels": labels,
            "detail_list_cost": list_cost if detail_list_cost is None else detail_list_cost,
            "fallback_list_cost": fallback_list_cost,
            "usage_seconds": usage_seconds,
            "list_cost": list_cost,
        },
    )


def test_billing_summary_table_detection_on_sqlite() -> None:
    engine = _engine()
    with engine.begin() as connection:
        assert not _cost_billing_summary_table_exists(connection)
        connection.execute(text("CREATE TABLE cost_bq_export_summary_daily (id INTEGER)"))
        assert _cost_billing_summary_table_exists(connection)


def test_no_owner_resource_read_uses_only_published_serving_rows() -> None:
    engine = _engine()
    with engine.begin() as connection:
        _publish(connection, "2026-08-10")
        _serving_row(
            connection,
            usage_date="2026-08-10",
            resource_group_key="group-1",
            resource_key="detail-1",
            resource_name="instance-1",
            resource_id="instance-1",
            service_name="Compute Engine",
            labels='{"cluster":"prow"}',
            usage_seconds=40,
            list_cost=40,
        )
        _serving_row(
            connection,
            usage_date="2026-08-10",
            resource_group_key="group-2",
            resource_key="fallback-1",
            resource_name="(resource detail unavailable)",
            resource_id=None,
            service_name="Cloud Storage",
            labels=None,
            usage_seconds=0,
            list_cost=60,
            detail_list_cost=0,
            fallback_list_cost=60,
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


def test_resource_drilldown_aggregates_ids_labels_services_and_pages() -> None:
    engine = _engine()
    with engine.begin() as connection:
        for usage_date in ("2026-08-10", "2026-08-11"):
            _publish(connection, usage_date)
        _serving_row(
            connection,
            usage_date="2026-08-10",
            resource_group_key="ec2",
            resource_key="ec2-a",
            resource_name="i-0123456789abcdef0",
            resource_id="i-0123456789abcdef0",
            service_name="Zulu Service",
            labels='{"cluster":"first"}',
            usage_seconds=10,
            list_cost=4,
        )
        _serving_row(
            connection,
            usage_date="2026-08-11",
            resource_group_key="ec2",
            resource_key="ec2-b",
            resource_name="i-0123456789abcdef0",
            resource_id="i-0123456789abcdef0",
            service_name="Alpha Service",
            labels='{"cluster":"largest"}',
            usage_seconds=None,
            list_cost=-6,
        )
        _serving_row(
            connection,
            usage_date="2026-08-10",
            resource_group_key="s3",
            resource_key="s3-a",
            resource_name="bucket-name",
            resource_id=None,
            service_name="Cloud Storage",
            labels='{"Name":"bucket-name"}',
            usage_seconds=None,
            list_cost=3,
        )

    first_page = get_unmatched_resources(
        engine,
        _filters(end_date=date(2026, 8, 11)),
        page_size=1,
    )
    second_page = get_unmatched_resources(
        engine,
        _filters(end_date=date(2026, 8, 11)),
        page_size=1,
        cursor=first_page["meta"]["next_cursor"],
    )

    assert first_page["items"] == [
        {
            "resource_key": "s3",
            "resource_id": None,
            "resource_name": "bucket-name",
            "service_name": "Cloud Storage",
            "sku_name": "",
            "repo_name": "",
            "labels": "Name=bucket-name",
            "allocation_buckets": "",
            "first_seen_date": "",
            "last_seen_date": "",
            "observed_days": 0,
            "attribution_source": "",
            "attribution_status": "",
            "usage_seconds": None,
            "list_cost": 3.0,
            "resource_data_source": "resource_detail",
            "resource_detail_cost": 3.0,
        }
    ]
    assert second_page["items"][0]["resource_id"] == "i-0123456789abcdef0"
    assert second_page["items"][0]["service_name"] == "Alpha Service,Zulu Service"
    assert second_page["items"][0]["labels"] == "cluster=largest"
    assert second_page["items"][0]["list_cost"] == -2.0
    assert second_page["items"][0]["usage_seconds"] == 10.0
    assert second_page["meta"]["next_cursor"] is None

    duration_first_page = get_unmatched_resources(
        engine,
        _filters(end_date=date(2026, 8, 11)),
        sort_by="duration",
        page_size=1,
    )
    duration_second_page = get_unmatched_resources(
        engine,
        _filters(end_date=date(2026, 8, 11)),
        sort_by="duration",
        page_size=1,
        cursor=duration_first_page["meta"]["next_cursor"],
    )
    assert duration_first_page["items"][0]["resource_id"] == "i-0123456789abcdef0"
    assert duration_second_page["items"][0]["resource_name"] == "bucket-name"


def test_resource_read_is_native_only() -> None:
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO cost_resource_serving_publication (
              basis_key, vendor, account_id, usage_date, active_materialization_version,
              source_allocation_version, detail_list_cost, total_list_cost, source_row_count
            ) VALUES ('kubernetes_allocated', 'gcp', 'project-1', '2026-08-10',
              'v1', NULL, 100, 100, 1)
        """))
    result = get_unmatched_resources(engine, _filters())
    assert result["items"] == []
    assert result["meta"]["allocation_basis"] == "current_attribution"
    assert result["meta"]["pending_dates"] == ["2026-08-10"]


def test_publication_without_its_serving_rows_returns_pending() -> None:
    engine = _engine()
    with engine.begin() as connection:
        _publish(connection, "2026-08-10")

    result = get_unmatched_resources(engine, _filters())

    assert result["items"] == []
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("not json", "not json"),
        ("[]", "[]"),
        ('{"a":"","b":null,"nested":{"z":1},"tag":"value"}', 'nested={"z": 1}, tag=value'),
    ],
)
def test_resource_labels_are_safe_and_deterministic(value: str | None, expected: str) -> None:
    assert _format_vendor_labels(value) == expected


def test_resource_cursor_preserves_decimal_sort_values() -> None:
    cursor = _encode_resource_cursor(
        {
            "list_cost": Decimal("123456789012345.123456789"),
            "usage_seconds": Decimal("987654321.125"),
            "resource_group_key": "resource-1",
        },
        sort_by="list_cost",
    )

    assert _decode_resource_cursor(cursor, sort_by="list_cost") == {
        "list_cost": "123456789012345.123456789",
        "usage_is_null": False,
        "usage_seconds": "987654321.125",
        "resource_group_key": "resource-1",
    }


def test_resource_request_validates_pagination_and_normalizes_sort() -> None:
    with pytest.raises(ValueError, match="page_size"):
        get_unmatched_resources(_engine(), _filters(), page_size=0)
    with pytest.raises(ValueError, match="invalid resource cursor"):
        get_unmatched_resources(_engine(), _filters(), cursor="W10")  # base64url for []

    response = get_unmatched_resources(_engine(), _filters(), sort_by="unknown")
    assert response["meta"]["sort_by"] == "list_cost"


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
      resource_key TEXT, resource_name TEXT, resource_id TEXT, service_name TEXT,
      resource_identity_kind TEXT, representative_labels_json TEXT, metadata_variant_count INTEGER,
      detail_list_cost REAL, fallback_list_cost REAL, usage_seconds REAL, list_cost REAL,
      effective_cost REAL, credit_amount REAL, net_cost REAL, source_row_count INTEGER
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
