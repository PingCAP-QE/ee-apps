import hashlib
import json
from datetime import date

import pytest
from sqlalchemy import create_engine, text

import cost_insight.jobs.sync_gcp_unmatched_resources as gcp_unmatched_resources
from cost_insight.common.config import GcpBillingSettings
from cost_insight.common.row_utils import hash_value
from cost_insight.jobs import state_store
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_gcp_billing_summary import _normalize_summary_row
from cost_insight.jobs.sync_gcp_unmatched_resources import (
    JOB_NAME,
    _normalize_resource_row,
    build_unmatched_resource_row_hash,
    replace_unmatched_resource_usage_dates,
    run_sync_gcp_unmatched_resources,
)


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
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
                """
            )
        )
        connection.execute(
            text(
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
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE cost_resource_serving_publication (
                  basis_key TEXT,
                  vendor TEXT,
                  account_id TEXT,
                  usage_date TEXT,
                  PRIMARY KEY (basis_key, vendor, account_id, usage_date)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE cost_unmatched_resource_daily (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  vendor TEXT NOT NULL,
                  account_id TEXT NOT NULL,
                  billing_account_id TEXT,
                  export_partition_date TEXT NOT NULL,
                  usage_date TEXT NOT NULL,
                  region TEXT,
                  service_name TEXT,
                  sku_name TEXT,
                  namespace TEXT,
                  org TEXT,
                  repo TEXT,
                  target_branch TEXT,
                  vendor_tags_json TEXT,
                  author TEXT,
                  resource_name TEXT NOT NULL,
                  resource_id TEXT,
                  parent_resource_name TEXT,
                  source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
                  workload_name TEXT,
                  workload_type TEXT,
                  owner TEXT,
                  service TEXT,
                  project TEXT,
                  service_exec_id TEXT,
                  usage_seconds REAL,
                  list_cost REAL,
                  effective_cost REAL,
                  credit_amount REAL,
                  net_cost REAL,
                  source_export_time TEXT,
                  source_row_hash TEXT NOT NULL,
                  source_summary_row_hash TEXT,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(vendor, account_id, export_partition_date, source_row_hash)
                )
                """
            )
        )
    return engine


def _resource_row() -> dict[str, object]:
    return {
        "vendor": "gcp",
        "account_id": "pingcap-testing-account",
        "billing_account_id": "billing-1",
        "export_partition_date": "2026-05-20",
        "usage_date": "2026-05-18",
        "region": "us-central1",
        "service_name": "Compute Engine",
        "sku_name": "Core running",
        "namespace": "kube:unallocated",
        "author": "hawkingrei",
        "org": "pingcap",
        "repo": "tidb",
        "target_branch": "master",
        "vendor_tags_json": None,
        "resource_name": "tidb-test-pod-1",
        "summary_resource_name": "tidb-test-pod-1",
        "usage_seconds": "3600.00",
        "list_cost": "10.00",
        "effective_cost": "8.00",
        "credit_amount": "-1.00",
        "net_cost": "7.00",
        "source_export_time": "2026-05-20T01:02:03Z",
    }


def _legacy_unmatched_resource_row_hash(row: dict[str, object]) -> str:
    legacy_fields = (
        "vendor",
        "account_id",
        "billing_account_id",
        "export_partition_date",
        "usage_date",
        "service_name",
        "sku_name",
        "namespace",
        "author",
        "org",
        "repo",
        "target_branch",
        "resource_name",
    )
    payload = {field: hash_value(row.get(field)) for field in legacy_fields}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_replace_unmatched_resource_usage_dates_keeps_existing_rows_for_empty_source() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")

    try:
        run_sync_gcp_unmatched_resources(
            engine,
            settings=settings,
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )

        rows_written = replace_unmatched_resource_usage_dates(
            engine,
            [],
            row_count=0,
            vendor="gcp",
            account_id="pingcap-testing-account",
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            dry_run=False,
            batch_size=100,
        )

        with engine.begin() as connection:
            remaining_rows = connection.execute(
                text("SELECT COUNT(*) FROM cost_unmatched_resource_daily")
            ).scalar_one()
        assert rows_written == 0
        assert remaining_rows == 1
    finally:
        engine.dispose()


def test_resource_replacement_invalidates_the_full_serving_window() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_resource_serving_publication
                    VALUES ('native', 'gcp', 'pingcap-testing-account', '2026-05-18')
                    """
                )
            )
        replace_unmatched_resource_usage_dates(
            engine,
            [_normalize_resource_row(_resource_row())],
            row_count=1,
            vendor="gcp",
            account_id="pingcap-testing-account",
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            dry_run=False,
            batch_size=100,
        )
        with engine.begin() as connection:
            publication_count = connection.execute(
                text("SELECT COUNT(*) FROM cost_resource_serving_publication")
            ).scalar_one()
        assert publication_count == 0
    finally:
        engine.dispose()


def test_resource_replacement_keeps_partial_raw_rows_unpublished_and_retries_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _sqlite_engine()
    rows = [
        _normalize_resource_row({**_resource_row(), "resource_name": f"tidb-test-pod-{index}"})
        for index in range(5)
    ]
    original_write = gcp_unmatched_resources._write_unmatched_resource_rows
    write_calls = 0

    def fail_on_second_transaction(*args, **kwargs):
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            raise ConnectionError("simulated TiDB connection loss")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(gcp_unmatched_resources, "_write_unmatched_resource_rows", fail_on_second_transaction)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_resource_serving_publication
                    VALUES ('native', 'gcp', 'pingcap-testing-account', '2026-05-18')
                    """
                )
            )

        with pytest.raises(ConnectionError, match="simulated TiDB connection loss"):
            replace_unmatched_resource_usage_dates(
                engine,
                rows,
                row_count=len(rows),
                vendor="gcp",
                account_id="pingcap-testing-account",
                usage_start_date=date(2026, 5, 18),
                usage_end_date=date(2026, 5, 18),
                dry_run=False,
                batch_size=10,
                transaction_row_limit=2,
            )

        with engine.begin() as connection:
            partial_rows = connection.execute(
                text("SELECT COUNT(*) FROM cost_unmatched_resource_daily")
            ).scalar_one()
            publication_count = connection.execute(
                text("SELECT COUNT(*) FROM cost_resource_serving_publication")
            ).scalar_one()
        assert partial_rows == 2
        assert publication_count == 0

        monkeypatch.setattr(gcp_unmatched_resources, "_write_unmatched_resource_rows", original_write)
        rows_written = replace_unmatched_resource_usage_dates(
            engine,
            rows,
            row_count=len(rows),
            vendor="gcp",
            account_id="pingcap-testing-account",
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            dry_run=False,
            batch_size=10,
            transaction_row_limit=2,
        )
        with engine.begin() as connection:
            row_count = connection.execute(
                text("SELECT COUNT(*) FROM cost_unmatched_resource_daily")
            ).scalar_one()
        assert rows_written == len(rows)
        assert row_count == len(rows)
    finally:
        engine.dispose()


def test_resource_replacement_remains_atomic_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _sqlite_engine()
    original_write = gcp_unmatched_resources._write_unmatched_resource_rows

    def fail_write(*args, **kwargs):
        raise ConnectionError("simulated TiDB connection loss")

    try:
        run_sync_gcp_unmatched_resources(
            engine,
            settings=GcpBillingSettings(account_id="pingcap-testing-account"),
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_resource_serving_publication
                    VALUES ('native', 'gcp', 'pingcap-testing-account', '2026-05-18')
                    """
                )
            )
        monkeypatch.setattr(gcp_unmatched_resources, "_write_unmatched_resource_rows", fail_write)

        with pytest.raises(ConnectionError, match="simulated TiDB connection loss"):
            replace_unmatched_resource_usage_dates(
                engine,
                [_normalize_resource_row({**_resource_row(), "resource_name": "replacement"})],
                row_count=1,
                vendor="gcp",
                account_id="pingcap-testing-account",
                usage_start_date=date(2026, 5, 18),
                usage_end_date=date(2026, 5, 18),
                dry_run=False,
                batch_size=10,
            )

        with engine.begin() as connection:
            row_count = connection.execute(
                text("SELECT COUNT(*) FROM cost_unmatched_resource_daily")
            ).scalar_one()
            publication_count = connection.execute(
                text("SELECT COUNT(*) FROM cost_resource_serving_publication")
            ).scalar_one()
        assert row_count == 1
        assert publication_count == 1
    finally:
        monkeypatch.setattr(gcp_unmatched_resources, "_write_unmatched_resource_rows", original_write)
        engine.dispose()


def test_gcp_resource_lineage_matches_the_summary_identity() -> None:
    resource = _normalize_resource_row(
        {**_resource_row(), "vendor_tags_json": {"cluster": "prow"}}
    )
    summary = _normalize_summary_row(
        {
            **_resource_row(),
            "usage_type": None,
            "resource_name": resource["summary_resource_name"],
            "vendor_tags_json": None,
        }
    )

    assert resource["source_summary_row_hash"] == summary["source_row_hash"]


def test_aws_resource_lineage_uses_compact_tags_when_full_labels_exist() -> None:
    resource = _normalize_resource_row(
        {
            **_resource_row(),
            "vendor": "aws",
            "summary_vendor_tags_json": None,
            "vendor_tags_json": '{"Name":"runner-a"}',
        }
    )
    summary = _normalize_summary_row(
        {
            **_resource_row(),
            "vendor": "aws",
            "usage_type": None,
            "resource_name": resource["summary_resource_name"],
            "vendor_tags_json": None,
        }
    )

    assert resource["source_summary_row_hash"] == summary["source_row_hash"]


def test_aws_resource_hash_uses_summary_tags_not_full_labels() -> None:
    row = {
        **_resource_row(),
        "vendor": "aws",
        "summary_vendor_tags_json": '{"cluster":"prow"}',
        "vendor_tags_json": '{"Name":"runner-a","cluster":"prow"}',
    }

    assert build_unmatched_resource_row_hash(row) == build_unmatched_resource_row_hash(
        {**row, "vendor_tags_json": '{"Name":"runner-b","cluster":"prow"}'}
    )


def test_unmatched_resource_hash_ignores_amount_changes() -> None:
    row = _normalize_resource_row(_resource_row())
    changed = {**row, "net_cost": "99.00"}

    assert build_unmatched_resource_row_hash(row) != _legacy_unmatched_resource_row_hash(row)
    assert build_unmatched_resource_row_hash(row) == build_unmatched_resource_row_hash(changed)
    assert build_unmatched_resource_row_hash(row) != build_unmatched_resource_row_hash(
        {**row, "target_branch": "release-8.5"}
    )
    assert build_unmatched_resource_row_hash(row) != build_unmatched_resource_row_hash(
        {
            **row,
            "vendor_tags_json": '{"cluster":"10149878793099322221","shared_pool":"2076551309477019648"}',
        }
    )
    labeled = _normalize_resource_row(
        {**_resource_row(), "vendor_tags_json": {"cluster": "prow"}}
    )
    # GCP summary intentionally rolls labels up, so labels only split detail rows.
    assert row["source_summary_row_hash"] == labeled["source_summary_row_hash"]
    assert row["source_row_hash"] != labeled["source_row_hash"]
    changed_region = _normalize_resource_row({**_resource_row(), "region": "us-east1"})
    assert row["source_summary_row_hash"] != changed_region["source_summary_row_hash"]
    assert build_unmatched_resource_row_hash(row) != build_unmatched_resource_row_hash(
        changed_region
    )
    assert build_unmatched_resource_row_hash(row) != build_unmatched_resource_row_hash(
        {**row, "source_summary_row_hash": "replacement-lineage"}
    )


def test_normalize_resource_row_rejects_missing_resource_name() -> None:
    row = {**_resource_row(), "resource_name": ""}

    with pytest.raises(ValueError, match="Missing resource_name"):
        _normalize_resource_row(row)


def test_run_sync_gcp_unmatched_resources_writes_rows() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account", page_size=1)

    try:
        summary = run_sync_gcp_unmatched_resources(
            engine,
            settings=settings,
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )

        assert summary.export_partition_start == date(2026, 5, 18)
        assert summary.export_partition_end == date(2026, 5, 23)
        assert summary.rows_seen == 1
        assert summary.rows_written == 1
        with engine.begin() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM cost_unmatched_resource_daily")
            ).scalar_one()
            state = state_store.get_job_state(
                connection,
                source_job_name(JOB_NAME, vendor="gcp", account_id=settings.account_id),
            )
        assert count == 1
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_sync_gcp_unmatched_resources_caps_database_write_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account", page_size=11)
    batch_sizes: list[int] = []
    invalidation_batch_sizes: list[int] = []
    original_write = gcp_unmatched_resources._write_unmatched_resource_rows
    original_invalidate = gcp_unmatched_resources._invalidate_resource_serving_publications

    def record_write(*args, **kwargs):
        batch_sizes.append(len(args[1]))
        return original_write(*args, **kwargs)

    def record_invalidate(*args, **kwargs):
        invalidation_batch_sizes.append(len(args[1]))
        return original_invalidate(*args, **kwargs)

    rows = [{**_resource_row(), "resource_name": f"tidb-test-pod-{index}"} for index in range(11)]
    monkeypatch.setattr(gcp_unmatched_resources, "_write_unmatched_resource_rows", record_write)
    monkeypatch.setattr(
        gcp_unmatched_resources, "_invalidate_resource_serving_publications", record_invalidate
    )
    try:
        summary = run_sync_gcp_unmatched_resources(
            engine,
            settings=settings,
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            fetch_rows=lambda **_kwargs: rows,
        )

        assert summary.rows_written == 11
        assert batch_sizes == [10, 1]
        assert invalidation_batch_sizes == [11]
    finally:
        engine.dispose()


def test_run_sync_gcp_unmatched_resources_removes_superseded_unlabeled_row() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    labeled_row = {
        **_resource_row(),
        "vendor_tags_json": {
            "shared_pool": "2076551309477019648",
            "cluster": "10149878793099322221",
        },
    }

    try:
        run_sync_gcp_unmatched_resources(
            engine,
            settings=settings,
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )
        run_sync_gcp_unmatched_resources(
            engine,
            settings=settings,
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [labeled_row],
        )

        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT vendor_tags_json, ROUND(SUM(net_cost), 2) AS net_cost
                    FROM cost_unmatched_resource_daily
                    GROUP BY vendor_tags_json
                    """
                )
            ).all()
        assert rows == [
            ('{"cluster":"10149878793099322221","shared_pool":"2076551309477019648"}', 7.0)
        ]
    finally:
        engine.dispose()


def test_resource_identifier_enrichment_updates_the_existing_raw_row() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")
    try:
        run_sync_gcp_unmatched_resources(
            engine,
            settings=settings,
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )
        run_sync_gcp_unmatched_resources(
            engine,
            settings=settings,
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 18),
            fetch_rows=lambda **_kwargs: [{**_resource_row(), "resource_id": "projects/p/instances/i-1"}],
        )
        with engine.begin() as connection:
            count, resource_id = connection.execute(
                text("SELECT COUNT(*), MAX(resource_id) FROM cost_unmatched_resource_daily")
            ).one()
        assert (count, resource_id) == (1, "projects/p/instances/i-1")
    finally:
        engine.dispose()


def test_gcp_resource_sync_rematerializes_its_source_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _sqlite_engine()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        gcp_unmatched_resources,
        "run_materialize_resource_serving",
        lambda _engine, **kwargs: calls.append(kwargs),
    )
    try:
        run_sync_gcp_unmatched_resources(
            engine,
            settings=GcpBillingSettings(account_id="pingcap-testing-account"),
            usage_start_date=date(2026, 5, 18),
            usage_end_date=date(2026, 5, 19),
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )
        assert calls == [
            {
                "start_date": date(2026, 5, 18),
                "end_date": date(2026, 5, 19),
                "vendor": "gcp",
                "account_id": "pingcap-testing-account",
            }
        ]
    finally:
        engine.dispose()


def test_run_sync_gcp_unmatched_resources_rejects_invalid_range() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")

    try:
        with pytest.raises(ValueError, match="usage_start_date"):
            run_sync_gcp_unmatched_resources(
                engine,
                settings=settings,
                usage_start_date=date(2026, 5, 19),
                usage_end_date=date(2026, 5, 18),
                fetch_rows=lambda **_kwargs: [],
            )
    finally:
        engine.dispose()
