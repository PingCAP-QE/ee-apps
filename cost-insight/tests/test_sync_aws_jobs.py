from datetime import date

import pytest
from sqlalchemy import create_engine, text

import cost_insight.jobs.sync_aws_unmatched_resources as aws_unmatched_resources
import cost_insight.jobs.sync_gcp_unmatched_resources as gcp_unmatched_resources
from cost_insight.common.config import AwsBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_aws_billing_summary import (
    AWS_SPLIT_COST_SCHEMA_VERSION,
    AwsBillingSource,
    JOB_NAME as SUMMARY_JOB_NAME,
    _add_months,
    _month_floor,
    _start_partition_from_state,
    _watermark as summary_watermark,
    run_sync_aws_billing_summary,
)
from cost_insight.jobs.sync_aws_unmatched_resources import (
    JOB_NAME as UNMATCHED_JOB_NAME,
    _watermark as unmatched_watermark,
    run_sync_aws_unmatched_resources,
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
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              vendor TEXT NOT NULL,
              account_id TEXT NOT NULL,
              billing_account_id TEXT,
              export_partition_date TEXT NOT NULL,
              usage_date TEXT NOT NULL,
              service_name TEXT,
              sku_name TEXT,
              usage_type TEXT,
              cost_driver_key TEXT,
              region TEXT,
              org TEXT,
              repo TEXT,
              target_branch TEXT,
              resource_name TEXT,
              vendor_tags_json TEXT,
              author TEXT,
              source_schema_version TEXT,
              source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
              cluster_name TEXT,
              cluster_location TEXT,
              kubernetes_cost_class TEXT,
              kubernetes_residual_type TEXT,
              kubernetes_cost_component TEXT,
              namespace TEXT,
              workload_name TEXT,
              workload_type TEXT,
              owner TEXT,
              service TEXT,
              project TEXT,
              service_exec_id TEXT,
              list_cost REAL,
              effective_cost REAL,
              credit_amount REAL,
              net_cost REAL,
              source_export_time TEXT,
              source_row_hash TEXT NOT NULL,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(vendor, account_id, export_partition_date, source_row_hash)
            )
            """,
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
            """,
        ):
            connection.execute(text(statement))
    return engine


def _summary_row(day: str = "2026-05-01") -> dict[str, object]:
    return {
        "vendor": "aws",
        "account_id": "946646677266",
        "billing_account_id": "payer-1",
        "export_partition_date": "2026-05-01",
        "usage_date": day,
        "service_name": "Amazon Elastic Compute Cloud",
        "sku_name": "EBS:VolumeUsage.gp3",
        "usage_type": "USE1-EBS:VolumeUsage.gp3",
        "region": "us-east-1",
        "author": "test-infra",
        "org": "qe",
        "repo": "test-infra",
        "list_cost": "10.00",
        "effective_cost": "8.00",
        "credit_amount": "-1.00",
        "net_cost": "7.00",
        "source_export_time": "2026-05-02T01:02:03Z",
    }


def test_split_source_profile_selects_its_table_and_available_date() -> None:
    engine = _sqlite_engine()
    seen: dict[str, object] = {}

    def fetch_rows(**kwargs):
        seen.update(kwargs)
        return []

    try:
        result = run_sync_aws_billing_summary(
            engine,
            settings=AwsBillingSettings(account_id="946646677266"),
            account_id="946646677266",
            export_partition_start=date(2026, 8, 1),
            export_partition_end=date(2026, 8, 1),
            dry_run=True,
            source=AwsBillingSource(
                account_id="946646677266",
                billing_table="pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost",
                schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
                available_from=date(2026, 8, 2),
            ),
            fetch_rows=fetch_rows,
        )
    finally:
        engine.dispose()

    assert result.rows_seen == 0
    assert seen["billing_table"] == "pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"
    assert seen["earliest_usage_date"] == date(2026, 8, 2)


def _resource_row() -> dict[str, object]:
    return {
        "vendor": "aws",
        "account_id": "946646677266",
        "billing_account_id": "payer-1",
        "export_partition_date": "2026-05-01",
        "usage_date": "2026-05-01",
        "service_name": "Amazon Elastic Compute Cloud",
        "sku_name": "BoxUsage:c7g.2xlarge",
        "namespace": None,
        "author": "test-infra",
        "org": "qe",
        "repo": "test-infra",
        "resource_name": "i-0123456789abcdef0",
        "usage_seconds": "3600.00",
        "list_cost": "12.00",
        "effective_cost": "9.00",
        "credit_amount": "-1.00",
        "net_cost": "8.00",
        "source_export_time": "2026-05-02T01:02:03Z",
    }


def test_aws_summary_partition_helpers() -> None:
    assert _month_floor(date(2026, 5, 17)) == date(2026, 5, 1)
    assert _add_months(date(2026, 1, 1), 2) == date(2026, 3, 1)
    assert _start_partition_from_state(
        {"export_partition_end": "2026-05-01"},
        end_date=date(2026, 6, 1),
        overlap_months=1,
        initial_lookback_months=2,
    ) == date(2026, 5, 1)
    assert _start_partition_from_state(
        {},
        end_date=date(2026, 6, 1),
        overlap_months=1,
        initial_lookback_months=2,
    ) == date(2026, 5, 1)
    assert _start_partition_from_state(
        {},
        end_date=date(2026, 6, 1),
        overlap_months=1,
        initial_lookback_months=None,
    ) == date(2026, 6, 1)
    assert summary_watermark(
        account_id="946646677266",
        export_partition_start=date(2026, 5, 1),
        export_partition_end=date(2026, 5, 1),
    ) == {
        "vendor": "aws",
        "account_id": "946646677266",
        "export_partition_start": "2026-05-01",
        "export_partition_end": "2026-05-01",
    }
    assert unmatched_watermark(
        account_id="946646677266",
        usage_start_date=date(2026, 5, 1),
        usage_end_date=date(2026, 5, 2),
        export_partition_start=date(2026, 5, 1),
        export_partition_end=date(2026, 5, 1),
    ) == {
        "vendor": "aws",
        "account_id": "946646677266",
        "usage_start_date": "2026-05-01",
        "usage_end_date": "2026-05-02",
        "export_partition_start": "2026-05-01",
        "export_partition_end": "2026-05-01",
    }


def test_run_sync_aws_billing_summary_writes_rows_and_touched_dates() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=2)

    try:
        summary = run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-01"), _summary_row("2026-05-02")],
        )

        assert summary.rows_seen == 2
        assert summary.rows_written == 2
        assert summary.touched_usage_dates == (date(2026, 5, 1), date(2026, 5, 2))
        with engine.begin() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")).scalar_one()
            driver_rows = connection.execute(
                text(
                    """
                    SELECT DISTINCT usage_type, cost_driver_key
                    FROM cost_bq_export_summary_daily
                    """
                )
            ).all()
            state = state_store.get_job_state(
                connection,
                source_job_name(SUMMARY_JOB_NAME, vendor="aws", account_id="946646677266"),
            )
            source = connection.execute(
                text(
                    """
                    SELECT billing_account_id
                    FROM cost_sources
                    WHERE vendor = 'aws' AND account_id = '946646677266'
                    """
                )
            ).scalar_one()
        assert count == 2
        assert driver_rows == [("USE1-EBS:VolumeUsage.gp3", "block_storage")]
        assert state is not None
        assert state.last_status == "succeeded"
        assert source == "payer-1"
    finally:
        engine.dispose()


def test_run_sync_aws_billing_summary_can_replace_existing_partitions() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=2)

    try:
        run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-01"), _summary_row("2026-05-02")],
        )

        summary = run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            replace_existing_partitions=True,
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-03")],
        )

        assert summary.rows_seen == 1
        assert summary.rows_written == 1
        assert summary.touched_usage_dates == (date(2026, 5, 3),)
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT usage_date
                    FROM cost_bq_export_summary_daily
                    WHERE vendor = 'aws'
                      AND account_id = '946646677266'
                      AND export_partition_date = '2026-05-01'
                    ORDER BY usage_date
                    """
                )
            ).scalars().all()
        assert rows == ["2026-05-03"]
    finally:
        engine.dispose()


def test_regular_split_summary_sync_replaces_changed_hashes() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=2)
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 5, 1),
    )
    try:
        run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id=source.account_id,
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            source=source,
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-01"), _summary_row("2026-05-02")],
        )

        replacement = _summary_row("2026-05-02")
        replacement["source_allocation_scope"] = "eks_pod"
        replacement["namespace"] = "default"
        replacement["list_cost"] = "12.00"
        result = run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id=source.account_id,
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            usage_start_date=date(2026, 5, 1),
            usage_end_date=date(2026, 5, 2),
            source=source,
            fetch_rows=lambda **_kwargs: [replacement],
        )

        assert result.touched_usage_dates == (date(2026, 5, 2),)
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT usage_date, source_allocation_scope, list_cost
                    FROM cost_bq_export_summary_daily
                    ORDER BY usage_date, source_allocation_scope
                    """
                )
            ).all()
        assert rows == [
            ("2026-05-01", "direct", 10.0),
            ("2026-05-02", "eks_pod", 12.0),
        ]
    finally:
        engine.dispose()


def test_split_summary_replacement_only_deletes_requested_usage_dates() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=2)
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 5, 1),
    )
    try:
        run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id=source.account_id,
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            source=source,
            fetch_rows=lambda **_kwargs: [_summary_row("2026-05-01"), _summary_row("2026-05-02")],
        )

        replacement = _summary_row("2026-05-02")
        replacement["list_cost"] = "12.34567891"
        result = run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id=source.account_id,
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            source=source,
            replace_existing_usage_dates=True,
            usage_start_date=date(2026, 5, 2),
            usage_end_date=date(2026, 5, 2),
            fetch_rows=lambda **_kwargs: [replacement],
        )

        assert result.touched_usage_dates == (date(2026, 5, 2),)
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT usage_date, list_cost
                    FROM cost_bq_export_summary_daily
                    ORDER BY usage_date
                    """
                )
            ).all()
        assert rows == [("2026-05-01", 10.0), ("2026-05-02", 12.34567891)]
    finally:
        engine.dispose()


def test_split_summary_replacement_rejects_limited_source() -> None:
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
    )

    with pytest.raises(ValueError, match="cannot be used with limit"):
        run_sync_aws_billing_summary(
            object(),
            settings=AwsBillingSettings(account_id=source.account_id),
            account_id=source.account_id,
            limit=1,
            replace_existing_usage_dates=True,
            usage_start_date=date(2026, 5, 2),
            usage_end_date=date(2026, 5, 2),
            source=source,
        )


def test_split_summary_replacement_rejects_empty_source() -> None:
    engine = _sqlite_engine()
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 5, 1),
    )
    try:
        with pytest.raises(ValueError, match="source returned no rows"):
            run_sync_aws_billing_summary(
                engine,
                settings=AwsBillingSettings(account_id=source.account_id),
                account_id=source.account_id,
                export_partition_start=date(2026, 5, 1),
                export_partition_end=date(2026, 5, 1),
                source=source,
                replace_existing_usage_dates=True,
                usage_start_date=date(2026, 5, 2),
                usage_end_date=date(2026, 5, 2),
                fetch_rows=lambda **_kwargs: [],
            )
    finally:
        engine.dispose()


def test_split_summary_replacement_rejects_late_earliest_usage_date() -> None:
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
    )

    with pytest.raises(ValueError, match="earliest_usage_date"):
        run_sync_aws_billing_summary(
            object(),
            settings=AwsBillingSettings(account_id=source.account_id),
            account_id=source.account_id,
            earliest_usage_date=date(2026, 5, 3),
            replace_existing_usage_dates=True,
            usage_start_date=date(2026, 5, 2),
            usage_end_date=date(2026, 5, 2),
            source=source,
        )


def test_run_sync_aws_billing_summary_dry_run_and_failure() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266")

    def raise_fetch(**_kwargs):
        raise RuntimeError("boom")
        yield

    try:
        summary = run_sync_aws_billing_summary(
            engine,
            settings=settings,
            account_id="946646677266",
            export_partition_start=date(2026, 5, 1),
            export_partition_end=date(2026, 5, 1),
            dry_run=True,
            fetch_rows=lambda **_kwargs: [_summary_row()],
        )
        assert summary.rows_seen == 1
        assert summary.rows_written == 0

        with pytest.raises(RuntimeError, match="boom"):
            run_sync_aws_billing_summary(
                engine,
                settings=settings,
                account_id="946646677266",
                export_partition_start=date(2026, 5, 1),
                export_partition_end=date(2026, 5, 1),
                fetch_rows=raise_fetch,
            )

        with engine.begin() as connection:
            state = state_store.get_job_state(
                connection,
                source_job_name(SUMMARY_JOB_NAME, vendor="aws", account_id="946646677266"),
            )
        assert state is not None
        assert state.last_status == "failed"
    finally:
        engine.dispose()


def test_run_sync_aws_unmatched_resources_writes_rows() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=1)

    try:
        summary = run_sync_aws_unmatched_resources(
            engine,
            settings=settings,
            account_id="946646677266",
            usage_start_date=date(2026, 5, 1),
            usage_end_date=date(2026, 5, 2),
            dry_run=False,
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )

        assert summary.rows_seen == 1
        assert summary.rows_written == 1
        assert summary.export_partition_start == date(2026, 5, 1)
        assert summary.export_partition_end == date(2026, 5, 1)
        with engine.begin() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM cost_unmatched_resource_daily")
            ).scalar_one()
            state = state_store.get_job_state(
                connection,
                source_job_name(UNMATCHED_JOB_NAME, vendor="aws", account_id="946646677266"),
            )
        assert count == 1
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_sync_aws_unmatched_resources_caps_database_write_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=11)
    batch_sizes: list[int] = []
    original_write = gcp_unmatched_resources._write_unmatched_resource_rows

    def record_write(*args, **kwargs):
        batch_sizes.append(len(args[1]))
        return original_write(*args, **kwargs)

    rows = [{**_resource_row(), "resource_name": f"i-{index:016x}"} for index in range(11)]
    monkeypatch.setattr(gcp_unmatched_resources, "_write_unmatched_resource_rows", record_write)
    try:
        summary = run_sync_aws_unmatched_resources(
            engine,
            settings=settings,
            account_id="946646677266",
            usage_start_date=date(2026, 5, 1),
            usage_end_date=date(2026, 5, 1),
            fetch_rows=lambda **_kwargs: rows,
        )

        assert summary.rows_written == 11
        assert batch_sizes == [10, 1]
    finally:
        engine.dispose()


def test_split_unmatched_replacement_only_deletes_requested_usage_dates() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=2)
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 5, 1),
    )
    try:
        first = _resource_row()
        second = _resource_row()
        second["usage_date"] = "2026-05-02"
        second["resource_name"] = "i-0123456789abcdef1"
        run_sync_aws_unmatched_resources(
            engine,
            settings=settings,
            account_id=source.account_id,
            usage_start_date=date(2026, 5, 1),
            usage_end_date=date(2026, 5, 2),
            source=source,
            fetch_rows=lambda **_kwargs: [first, second],
        )

        replacement = _resource_row()
        replacement["usage_date"] = "2026-05-02"
        replacement["resource_name"] = "i-0123456789abcdef2"
        run_sync_aws_unmatched_resources(
            engine,
            settings=settings,
            account_id=source.account_id,
            usage_start_date=date(2026, 5, 2),
            usage_end_date=date(2026, 5, 2),
            source=source,
            replace_existing_usage_dates=True,
            fetch_rows=lambda **_kwargs: [replacement],
        )

        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT usage_date, resource_name
                    FROM cost_unmatched_resource_daily
                    ORDER BY usage_date, resource_name
                    """
                )
            ).all()
        assert rows == [
            ("2026-05-01", "i-0123456789abcdef0"),
            ("2026-05-02", "i-0123456789abcdef2"),
        ]
    finally:
        engine.dispose()


def test_split_unmatched_replacement_caps_database_write_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266", page_size=11)
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 5, 1),
    )
    batch_sizes: list[int] = []
    individual_invalidation_batch_sizes: list[int] = []
    original_write = gcp_unmatched_resources._write_unmatched_resource_rows
    original_invalidate = gcp_unmatched_resources._invalidate_resource_serving_publications

    def record_write(*args, **kwargs):
        batch_sizes.append(len(args[1]))
        return original_write(*args, **kwargs)

    def record_invalidate(*args, **kwargs):
        individual_invalidation_batch_sizes.append(len(args[1]))
        return original_invalidate(*args, **kwargs)

    rows = [{**_resource_row(), "resource_name": f"i-{index:016x}"} for index in range(11)]
    monkeypatch.setattr(gcp_unmatched_resources, "_write_unmatched_resource_rows", record_write)
    monkeypatch.setattr(
        gcp_unmatched_resources, "_invalidate_resource_serving_publications", record_invalidate
    )
    try:
        summary = run_sync_aws_unmatched_resources(
            engine,
            settings=settings,
            account_id=source.account_id,
            usage_start_date=date(2026, 5, 2),
            usage_end_date=date(2026, 5, 2),
            source=source,
            replace_existing_usage_dates=True,
            fetch_rows=lambda **_kwargs: rows,
        )

        assert summary.rows_written == 11
        assert batch_sizes == [10, 1]
        assert individual_invalidation_batch_sizes == []
    finally:
        engine.dispose()


def test_split_unmatched_replacement_rejects_empty_source() -> None:
    engine = _sqlite_engine()
    source = AwsBillingSource(
        account_id="946646677266",
        billing_table="project.dataset.split_cost",
        schema_version=AWS_SPLIT_COST_SCHEMA_VERSION,
        available_from=date(2026, 5, 1),
    )
    try:
        with pytest.raises(ValueError, match="source returned no rows"):
            run_sync_aws_unmatched_resources(
                engine,
                settings=AwsBillingSettings(account_id=source.account_id),
                account_id=source.account_id,
                usage_start_date=date(2026, 5, 2),
                usage_end_date=date(2026, 5, 2),
                source=source,
                replace_existing_usage_dates=True,
                fetch_rows=lambda **_kwargs: [],
            )
    finally:
        engine.dispose()


def test_aws_label_enrichment_updates_existing_raw_resource_row() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266")
    first_row = {
        **_resource_row(),
        "summary_vendor_tags_json": '{"cluster":"prow"}',
        "vendor_tags_json": '{"Name":"old-name","cluster":"prow"}',
    }
    updated_row = {**first_row, "vendor_tags_json": '{"Name":"new-name","cluster":"prow"}'}
    try:
        for row in (first_row, updated_row):
            run_sync_aws_unmatched_resources(
                engine,
                settings=settings,
                account_id="946646677266",
                usage_start_date=date(2026, 5, 1),
                usage_end_date=date(2026, 5, 1),
                fetch_rows=lambda **_kwargs: [row],
            )
        with engine.begin() as connection:
            count, vendor_tags_json = connection.execute(
                text("SELECT COUNT(*), MAX(vendor_tags_json) FROM cost_unmatched_resource_daily")
            ).one()
        assert (count, vendor_tags_json) == (1, '{"Name":"new-name","cluster":"prow"}')
    finally:
        engine.dispose()


def test_aws_resource_sync_rematerializes_its_source_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _sqlite_engine()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        aws_unmatched_resources,
        "run_materialize_resource_serving",
        lambda _engine, **kwargs: calls.append(kwargs),
    )
    try:
        run_sync_aws_unmatched_resources(
            engine,
            settings=AwsBillingSettings(account_id="946646677266"),
            account_id="946646677266",
            usage_start_date=date(2026, 5, 1),
            usage_end_date=date(2026, 5, 2),
            fetch_rows=lambda **_kwargs: [_resource_row()],
        )
        assert calls == [
            {
                "start_date": date(2026, 5, 1),
                "end_date": date(2026, 5, 2),
                "vendor": "aws",
                "account_id": "946646677266",
            }
        ]
    finally:
        engine.dispose()


def test_run_sync_aws_unmatched_resources_rejects_invalid_range_and_marks_failure() -> None:
    engine = _sqlite_engine()
    settings = AwsBillingSettings(account_id="946646677266")

    def raise_fetch(**_kwargs):
        raise RuntimeError("fetch failed")
        yield

    try:
        with pytest.raises(ValueError, match="usage_start_date"):
            run_sync_aws_unmatched_resources(
                engine,
                settings=settings,
                account_id="946646677266",
                usage_start_date=date(2026, 5, 2),
                usage_end_date=date(2026, 5, 1),
                fetch_rows=lambda **_kwargs: [],
            )

        with pytest.raises(RuntimeError, match="fetch failed"):
            run_sync_aws_unmatched_resources(
                engine,
                settings=settings,
                account_id="946646677266",
                usage_start_date=date(2026, 5, 1),
                usage_end_date=date(2026, 5, 1),
                fetch_rows=raise_fetch,
            )

        with engine.begin() as connection:
            state = state_store.get_job_state(
                connection,
                source_job_name(UNMATCHED_JOB_NAME, vendor="aws", account_id="946646677266"),
            )
        assert state is not None
        assert state.last_status == "failed"
    finally:
        engine.dispose()
