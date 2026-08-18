from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
import tempfile
from sqlalchemy import text
from sqlalchemy.engine import Engine

from cost_insight.common.config import AwsBillingSettings
from cost_insight.jobs.sync_gcp_billing_summary import (
    _dump_spooled_row,
    _iter_spooled_rows,
    _normalize_summary_row,
    replace_summary_usage_dates,
)
from cost_insight.jobs.sync_gcp_unmatched_resources import (
    _normalize_resource_row,
    replace_unmatched_resource_usage_dates,
)
from cost_insight.sources.aws_split_cost_export import (
    fetch_aws_split_cost_summary_rows,
    fetch_aws_split_cost_unmatched_resource_rows,
)

LOG = logging.getLogger(__name__)

SHADOW_WINDOW_ID = "aws-7266-20260802-20260815"
AWS_7266_ACCOUNT_ID = "946646677266"
AWS_7266_SPLIT_COST_TABLE = "pingcap-testing-account.multicloud_cur.ods_aws_946646677266_split_cost"


@dataclass(frozen=True)
class AwsSplitCostShadowTarget:
    window_id: str
    usage_start_date: date
    usage_end_date: date
    legacy_summary_snapshot_table: str
    split_summary_shadow_table: str
    legacy_unmatched_snapshot_table: str
    split_unmatched_shadow_table: str


AWS_7266_SHADOW_TARGET = AwsSplitCostShadowTarget(
    window_id=SHADOW_WINDOW_ID,
    usage_start_date=date(2026, 8, 2),
    usage_end_date=date(2026, 8, 15),
    # TiDB table identifiers are limited to 64 characters.
    legacy_summary_snapshot_table="cost_summary_aws_7266_legacy_20260802_20260815",
    split_summary_shadow_table="cost_summary_aws_7266_split_20260802_20260815",
    legacy_unmatched_snapshot_table="cost_unmatched_aws_7266_legacy_20260802_20260815",
    split_unmatched_shadow_table="cost_unmatched_aws_7266_split_20260802_20260815",
)

_SHADOW_TARGETS = {AWS_7266_SHADOW_TARGET.window_id: AWS_7266_SHADOW_TARGET}


@dataclass(frozen=True)
class AwsSplitCostShadowResult:
    window_id: str
    summary_rows_seen: int
    summary_rows_written: int
    unmatched_rows_seen: int
    unmatched_rows_written: int
    dry_run: bool


def resolve_aws_split_cost_shadow_target(window_id: str) -> AwsSplitCostShadowTarget:
    try:
        return _SHADOW_TARGETS[window_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported AWS split-cost shadow window: {window_id!r}") from exc


def snapshot_aws_split_cost_shadow_legacy(
    engine: Engine,
    *,
    window_id: str = SHADOW_WINDOW_ID,
    include_unmatched_resources: bool = True,
    dry_run: bool = False,
) -> AwsSplitCostShadowTarget:
    """Create fixed legacy snapshots; the target is selected only from an allowlist."""
    target = resolve_aws_split_cost_shadow_target(window_id)
    if dry_run:
        return target
    with engine.begin() as connection:
        _clone_usage_window(
            connection,
            source_table="cost_bq_export_summary_daily",
            target_table=target.legacy_summary_snapshot_table,
            target=target,
        )
        if include_unmatched_resources:
            _clone_usage_window(
                connection,
                source_table="cost_unmatched_resource_daily",
                target_table=target.legacy_unmatched_snapshot_table,
                target=target,
            )
    return target


def run_aws_split_cost_shadow(
    engine: Engine,
    *,
    settings: AwsBillingSettings,
    window_id: str = SHADOW_WINDOW_ID,
    include_unmatched_resources: bool = True,
    dry_run: bool = False,
    limit: int | None = None,
    summary_fetch_rows=fetch_aws_split_cost_summary_rows,
    unmatched_fetch_rows=fetch_aws_split_cost_unmatched_resource_rows,
) -> AwsSplitCostShadowResult:
    """Import a fixed split-cost validation window without changing production state."""
    target = resolve_aws_split_cost_shadow_target(window_id)
    if not dry_run:
        with engine.begin() as connection:
            _ensure_table(
                connection,
                source_table="cost_bq_export_summary_daily",
                target_table=target.split_summary_shadow_table,
            )
            if include_unmatched_resources:
                _ensure_table(
                    connection,
                    source_table="cost_unmatched_resource_daily",
                    target_table=target.split_unmatched_shadow_table,
                )
    summary_rows_seen = 0
    with tempfile.TemporaryFile("w+b") as summary_spool:
        for source_row in summary_fetch_rows(
            billing_table=AWS_7266_SPLIT_COST_TABLE,
            account_id=AWS_7266_ACCOUNT_ID,
            export_partition_start=target.usage_start_date.replace(day=1),
            export_partition_end=target.usage_end_date.replace(day=1),
            earliest_usage_date=target.usage_start_date,
            usage_end_date=target.usage_end_date,
            page_size=settings.page_size,
            limit=limit,
        ):
            summary_rows_seen += 1
            _dump_spooled_row(
                summary_spool,
                _normalize_summary_row(
                    {**source_row, "source_schema_version": "aws_split_cost_v1"}
                ),
            )
        summary_rows_written = replace_summary_usage_dates(
            engine,
            _iter_spooled_rows(summary_spool),
            row_count=summary_rows_seen,
            vendor="aws",
            account_id=AWS_7266_ACCOUNT_ID,
            usage_start_date=target.usage_start_date,
            usage_end_date=target.usage_end_date,
            dry_run=dry_run,
            batch_size=settings.page_size,
            target_table=target.split_summary_shadow_table,
        )

    unmatched_rows_seen = 0
    unmatched_rows_written = 0
    if include_unmatched_resources:
        with tempfile.TemporaryFile("w+b") as unmatched_spool:
            for source_row in unmatched_fetch_rows(
                billing_table=AWS_7266_SPLIT_COST_TABLE,
                account_id=AWS_7266_ACCOUNT_ID,
                export_partition_start=target.usage_start_date.replace(day=1),
                export_partition_end=target.usage_end_date.replace(day=1),
                usage_start_date=target.usage_start_date,
                usage_end_date=target.usage_end_date,
                page_size=settings.page_size,
                limit=limit,
                validate_guardrail=False,
            ):
                unmatched_rows_seen += 1
                _dump_spooled_row(unmatched_spool, _normalize_resource_row(source_row))
            unmatched_rows_written = replace_unmatched_resource_usage_dates(
                engine,
                _iter_spooled_rows(unmatched_spool),
                row_count=unmatched_rows_seen,
                vendor="aws",
                account_id=AWS_7266_ACCOUNT_ID,
                usage_start_date=target.usage_start_date,
                usage_end_date=target.usage_end_date,
                dry_run=dry_run,
                batch_size=settings.page_size,
                target_table=target.split_unmatched_shadow_table,
            )

    return AwsSplitCostShadowResult(
        window_id=target.window_id,
        summary_rows_seen=summary_rows_seen,
        summary_rows_written=summary_rows_written,
        unmatched_rows_seen=unmatched_rows_seen,
        unmatched_rows_written=unmatched_rows_written,
        dry_run=dry_run,
    )


def _clone_usage_window(
    connection,
    *,
    source_table: str,
    target_table: str,
    target: AwsSplitCostShadowTarget,
) -> None:
    if _table_exists(connection, target_table):
        LOG.info(
            "legacy shadow snapshot already exists; preserving immutable snapshot",
            extra={"target_table": target_table},
        )
        return

    temporary_table = f"{target_table}_tmp"
    connection.execute(text(f"DROP TABLE IF EXISTS `{temporary_table}`"))
    try:
        connection.execute(text(f"CREATE TABLE `{temporary_table}` LIKE `{source_table}`"))
        connection.execute(
            text(
                f"""
                INSERT INTO `{temporary_table}`
                SELECT *
                FROM `{source_table}`
                WHERE vendor = 'aws'
                  AND account_id = :account_id
                  AND usage_date BETWEEN :usage_start_date AND :usage_end_date
                """
            ),
            {
                "account_id": AWS_7266_ACCOUNT_ID,
                "usage_start_date": target.usage_start_date,
                "usage_end_date": target.usage_end_date,
            },
        )
        connection.execute(text(f"RENAME TABLE `{temporary_table}` TO `{target_table}`"))
    except Exception:
        connection.execute(text(f"DROP TABLE IF EXISTS `{temporary_table}`"))
        raise


def _ensure_table(connection, *, source_table: str, target_table: str) -> None:
    connection.execute(text(f"CREATE TABLE IF NOT EXISTS `{target_table}` LIKE `{source_table}`"))


def _table_exists(connection, table: str) -> bool:
    return (
        connection.execute(
            _TABLE_EXISTS,
            {"table": table},
        ).first()
        is not None
    )


_TABLE_EXISTS = text(
    """
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = DATABASE()
      AND table_name = :table
    LIMIT 1
    """
)
