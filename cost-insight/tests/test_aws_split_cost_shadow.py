from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from cost_insight.common.config import AwsBillingSettings
from cost_insight.jobs.aws_split_cost_shadow import (
    AWS_7266_ACCOUNT_ID,
    AWS_7266_SHADOW_TARGET,
    SHADOW_WINDOW_ID,
    _clone_usage_window,
    _table_exists,
    resolve_aws_split_cost_shadow_target,
    run_aws_split_cost_shadow,
    snapshot_aws_split_cost_shadow_legacy,
)


class _FakeResult:
    def __init__(self, exists: bool = False) -> None:
        self.exists = exists

    def first(self):
        return (1,) if self.exists else None


class _RecordingConnection:
    def __init__(self, *, target_exists: bool = False, fail_insert: bool = False) -> None:
        self.target_exists = target_exists
        self.fail_insert = fail_insert
        self.executions: list[tuple[str, object]] = []
        self.dialect = SimpleNamespace(name="mysql")

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.executions.append((sql, parameters))
        if "information_schema.tables" in sql:
            return _FakeResult(self.target_exists)
        if self.fail_insert and "INSERT INTO" in sql:
            raise RuntimeError("insert failed")
        return _FakeResult()


class _RecordingEngine:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection

    @contextmanager
    def begin(self):
        yield self.connection


def _summary_row():
    return {
        "vendor": "aws",
        "account_id": "946646677266",
        "billing_account_id": "payer",
        "export_partition_date": "2026-08-01",
        "usage_date": "2026-08-10",
        "service_name": "Amazon EC2",
        "sku_name": "BoxUsage",
        "usage_type": "USE1-BoxUsage",
        "region": "us-east-1",
        "source_allocation_scope": "eks_pod",
        "namespace": "ns",
        "workload_name": "workload",
        "workload_type": "deployment",
        "owner": "owner@pingcap.com",
        "service": "svc",
        "project": "project",
        "service_exec_id": "exec",
        "author": "owner@pingcap.com",
        "org": "tenant",
        "repo": "project",
        "list_cost": "0.00000001",
        "effective_cost": "0.00000001",
        "credit_amount": "0",
        "net_cost": "0.00000001",
        "source_export_time": "2026-08-11T00:00:00Z",
    }


def _resource_row():
    row = _summary_row()
    row.update(
        {
            "resource_name": ":pod/example",
            "parent_resource_name": "i-0ef88ef97606efb63",
            "usage_seconds": "1",
        }
    )
    return row


def test_shadow_targets_are_allowlisted_and_dry_run_does_not_write_production() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        assert resolve_aws_split_cost_shadow_target(SHADOW_WINDOW_ID) == AWS_7266_SHADOW_TARGET
        assert snapshot_aws_split_cost_shadow_legacy(engine, dry_run=True) == AWS_7266_SHADOW_TARGET
        with pytest.raises(ValueError, match="Unsupported AWS split-cost shadow window"):
            resolve_aws_split_cost_shadow_target("cost_bq_export_summary_daily; DROP TABLE x")

        result = run_aws_split_cost_shadow(
            engine,
            settings=AwsBillingSettings(account_id="946646677266"),
            dry_run=True,
            summary_fetch_rows=lambda **_kwargs: [_summary_row()],
            unmatched_fetch_rows=lambda **_kwargs: [_resource_row()],
        )
        assert result.summary_rows_seen == 1
        assert result.summary_rows_written == 0
        assert result.unmatched_rows_seen == 1
        assert result.unmatched_rows_written == 0
    finally:
        engine.dispose()


def test_clone_usage_window_uses_information_schema_and_publishes_after_insert() -> None:
    connection = _RecordingConnection()

    _clone_usage_window(
        connection,
        source_table="cost_bq_export_summary_daily",
        target_table=AWS_7266_SHADOW_TARGET.legacy_summary_snapshot_table,
        target=AWS_7266_SHADOW_TARGET,
    )

    statements = [sql for sql, _ in connection.executions]
    parameters = [params for _, params in connection.executions]
    temporary_table = f"{AWS_7266_SHADOW_TARGET.legacy_summary_snapshot_table}_tmp"
    assert "information_schema.tables" in statements[0]
    assert parameters[0] == {"table": AWS_7266_SHADOW_TARGET.legacy_summary_snapshot_table}
    assert f"DROP TABLE IF EXISTS `{temporary_table}`" in statements[1]
    assert f"CREATE TABLE `{temporary_table}` LIKE `cost_bq_export_summary_daily`" in statements[2]
    assert f"INSERT INTO `{temporary_table}`" in statements[3]
    assert (
        f"RENAME TABLE `{temporary_table}` TO "
        f"`{AWS_7266_SHADOW_TARGET.legacy_summary_snapshot_table}`"
    ) in statements[4]


def test_clone_usage_window_cleans_temporary_table_when_insert_fails() -> None:
    connection = _RecordingConnection(fail_insert=True)

    with pytest.raises(RuntimeError, match="insert failed"):
        _clone_usage_window(
            connection,
            source_table="cost_bq_export_summary_daily",
            target_table=AWS_7266_SHADOW_TARGET.legacy_summary_snapshot_table,
            target=AWS_7266_SHADOW_TARGET,
        )

    statements = [sql for sql, _ in connection.executions]
    temporary_table = f"{AWS_7266_SHADOW_TARGET.legacy_summary_snapshot_table}_tmp"
    assert sum(f"DROP TABLE IF EXISTS `{temporary_table}`" in sql for sql in statements) == 2
    assert not any("RENAME TABLE" in sql for sql in statements)


def test_shadow_non_dry_run_creates_targets_and_writes_rows() -> None:
    connection = _RecordingConnection()
    engine = _RecordingEngine(connection)

    result = run_aws_split_cost_shadow(
        engine,
        settings=AwsBillingSettings(account_id=AWS_7266_ACCOUNT_ID),
        summary_fetch_rows=lambda **_kwargs: [_summary_row()],
        unmatched_fetch_rows=lambda **_kwargs: [_resource_row()],
    )

    statements = [sql for sql, _ in connection.executions]
    assert result.summary_rows_written == 1
    assert result.unmatched_rows_written == 1
    assert (
        f"CREATE TABLE IF NOT EXISTS `{AWS_7266_SHADOW_TARGET.split_summary_shadow_table}` "
        "LIKE `cost_bq_export_summary_daily`"
    ) in statements
    assert (
        f"CREATE TABLE IF NOT EXISTS `{AWS_7266_SHADOW_TARGET.split_unmatched_shadow_table}` "
        "LIKE `cost_unmatched_resource_daily`"
    ) in statements
    assert any(
        "INSERT INTO" in sql and f"`{AWS_7266_SHADOW_TARGET.split_summary_shadow_table}`" in sql
        for sql in statements
    )
    assert any(
        "INSERT INTO" in sql
        and f"`{AWS_7266_SHADOW_TARGET.split_unmatched_shadow_table}`" in sql
        for sql in statements
    )


def test_snapshot_retry_skips_existing_immutable_snapshot() -> None:
    connection = _RecordingConnection(target_exists=True)
    engine = _RecordingEngine(connection)

    snapshot_aws_split_cost_shadow_legacy(
        engine,
        include_unmatched_resources=False,
    )

    assert _table_exists(connection, AWS_7266_SHADOW_TARGET.legacy_summary_snapshot_table)
    statements = [sql for sql, _ in connection.executions]
    assert all("CREATE TABLE" not in sql for sql in statements)
    assert all("INSERT INTO" not in sql for sql in statements)
