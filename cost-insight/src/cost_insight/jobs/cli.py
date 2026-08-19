from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta

from cost_insight.common.config import AwsBillingSettings, GcpBillingSettings, get_settings
from cost_insight.common.db import build_engine
from cost_insight.common.logging import configure_logging
from cost_insight.jobs.backfill_cost_refine_from_raw import run_backfill_cost_refine_from_raw
from cost_insight.jobs.aws_split_cost_shadow import (
    AWS_7266_ACCOUNT_ID,
    SHADOW_WINDOW_ID,
    run_aws_split_cost_shadow,
    snapshot_aws_split_cost_shadow_legacy,
)
from cost_insight.jobs.bootstrap_gcs_cache_last_seen import run_bootstrap_gcs_cache_last_seen
from cost_insight.jobs.cleanup_gcs_cache import run_cleanup_gcs_cache
from cost_insight.jobs.cost_sources import list_active_cost_sources
from cost_insight.jobs.refresh_attribution_daily import (
    CostAttributionSource,
    run_refresh_cost_attribution_daily,
    run_refresh_cost_attribution_from_summary,
)
from cost_insight.jobs.sync_gcs_cache_last_seen import run_sync_gcs_cache_last_seen
from cost_insight.jobs.sync_aws_billing_summary import (
    AWS_CUR_LEGACY_SCHEMA_VERSION,
    AWS_SPLIT_COST_SCHEMA_VERSION,
    AwsBillingSource,
    run_sync_aws_billing_summary,
)
from cost_insight.jobs.sync_aws_parent_residual_allocations import (
    run_sync_aws_parent_residual_allocations,
)
from cost_insight.jobs.sync_aws_unmatched_resources import run_sync_aws_unmatched_resources
from cost_insight.jobs.sync_gcp_billing_summary import run_sync_gcp_billing_summary
from cost_insight.jobs.sync_gcp_billing_export import run_sync_gcp_billing_export
from cost_insight.jobs.sync_gcp_kubernetes_workload_allocations import (
    run_sync_gcp_kubernetes_workload_allocations,
)
from cost_insight.jobs.sync_gcp_unmatched_resources import run_sync_gcp_unmatched_resources
from cost_insight.jobs.sync_gcs_cache_ac_references import run_sync_gcs_cache_ac_references


class _ConnectionBoundEngine:
    """Expose one transaction through the Engine interface used by cutover jobs."""

    def __init__(self, connection) -> None:
        self._connection = connection

    @contextmanager
    def begin(self):
        yield self._connection


@contextmanager
def _atomic_cutover_engine(engine, *, dry_run: bool):
    if dry_run:
        yield engine
        return
    with engine.begin() as connection:
        yield _ConnectionBoundEngine(connection)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cost job runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_gcp = subparsers.add_parser(
        "sync-gcp-billing-export",
        help="Sync GCP detailed billing export into cost_raw_details",
    )
    sync_gcp.add_argument("--start-date", type=_parse_date, default=None)
    sync_gcp.add_argument("--end-date", type=_parse_date, default=None)
    sync_gcp.add_argument("--dry-run", action="store_true")
    sync_gcp.add_argument("--limit", type=int, default=None)
    sync_gcp.add_argument(
        "--replace-existing-dates",
        action="store_true",
        help="Delete existing GCP raw rows for the requested usage date range before importing.",
    )
    sync_gcp.add_argument(
        "--split-by-day",
        action="store_true",
        help="Run one usage date at a time; recommended for backfills.",
    )

    sync_summary = subparsers.add_parser(
        "sync-gcp-billing-summary",
        help="Sync GCP billing export partitions into cost_bq_export_summary_daily",
    )
    sync_summary.add_argument("--export-partition-start", type=_parse_date, default=None)
    sync_summary.add_argument("--export-partition-end", type=_parse_date, default=None)
    sync_summary.add_argument("--earliest-usage-date", type=_parse_date, default=None)
    sync_summary.add_argument("--dry-run", action="store_true")
    sync_summary.add_argument("--limit", type=int, default=None)
    sync_summary.add_argument(
        "--replace-existing-partitions",
        action="store_true",
        help="Delete existing GCP summary rows for the requested export partition range before importing.",
    )

    sync_aws_summary = subparsers.add_parser(
        "sync-aws-billing-summary",
        help="Sync AWS billing export partitions into cost_bq_export_summary_daily",
    )
    sync_aws_summary.add_argument("--export-partition-start", type=_parse_date, default=None)
    sync_aws_summary.add_argument("--export-partition-end", type=_parse_date, default=None)
    sync_aws_summary.add_argument("--earliest-usage-date", type=_parse_date, default=None)
    sync_aws_summary.add_argument("--dry-run", action="store_true")
    sync_aws_summary.add_argument("--limit", type=int, default=None)
    sync_aws_summary.add_argument(
        "--replace-existing-partitions",
        action="store_true",
        help="Delete existing AWS summary rows for the requested export partition range before importing.",
    )
    sync_aws_summary.add_argument(
        "--replace-existing-dates",
        action="store_true",
        help="Replace only the requested AWS split-cost usage dates.",
    )
    sync_aws_summary.add_argument("--usage-start-date", type=_parse_date, default=None)
    sync_aws_summary.add_argument("--usage-end-date", type=_parse_date, default=None)

    sync_unmatched = subparsers.add_parser(
        "sync-gcp-unmatched-resources",
        help="Sync weekly GCP resource-level rows for unmatched resource investigation",
    )
    sync_unmatched.add_argument("--usage-start-date", type=_parse_date, required=True)
    sync_unmatched.add_argument("--usage-end-date", type=_parse_date, required=True)
    sync_unmatched.add_argument("--export-partition-start", type=_parse_date, default=None)
    sync_unmatched.add_argument("--export-partition-end", type=_parse_date, default=None)
    sync_unmatched.add_argument("--dry-run", action="store_true")
    sync_unmatched.add_argument("--limit", type=int, default=None)

    sync_gke_allocations = subparsers.add_parser(
        "sync-gcp-kubernetes-workload-allocations",
        help="Allocate recognizable GKE node list cost to workloads using GKE metering",
    )
    sync_gke_allocations.add_argument("--usage-start-date", type=_parse_date, required=True)
    sync_gke_allocations.add_argument("--usage-end-date", type=_parse_date, required=True)
    sync_gke_allocations.add_argument("--export-partition-start", type=_parse_date, default=None)
    sync_gke_allocations.add_argument("--export-partition-end", type=_parse_date, default=None)
    sync_gke_allocations.add_argument("--dry-run", action="store_true")

    sync_aws_unmatched = subparsers.add_parser(
        "sync-aws-unmatched-resources",
        help="Sync weekly AWS resource-level rows for unmatched resource investigation",
    )
    sync_aws_unmatched.add_argument("--usage-start-date", type=_parse_date, required=True)
    sync_aws_unmatched.add_argument("--usage-end-date", type=_parse_date, required=True)
    sync_aws_unmatched.add_argument("--export-partition-start", type=_parse_date, default=None)
    sync_aws_unmatched.add_argument("--export-partition-end", type=_parse_date, default=None)
    sync_aws_unmatched.add_argument("--dry-run", action="store_true")
    sync_aws_unmatched.add_argument("--limit", type=int, default=None)
    sync_aws_unmatched.add_argument(
        "--replace-existing-dates",
        action="store_true",
        help="Replace only the requested AWS split-cost usage dates.",
    )

    snapshot_aws_shadow = subparsers.add_parser(
        "snapshot-aws-split-cost-shadow-legacy",
        help="Create the fixed legacy snapshot for the AWS 7266 split-cost shadow window.",
    )
    snapshot_aws_shadow.add_argument("--window-id", choices=(SHADOW_WINDOW_ID,), default=SHADOW_WINDOW_ID)
    snapshot_aws_shadow.add_argument("--skip-unmatched-resources", action="store_true")
    snapshot_aws_shadow.add_argument("--dry-run", action="store_true")

    sync_aws_shadow = subparsers.add_parser(
        "sync-aws-split-cost-shadow",
        help="Write the fixed AWS 7266 split-cost validation window to allowlisted shadow tables.",
    )
    sync_aws_shadow.add_argument("--window-id", choices=(SHADOW_WINDOW_ID,), default=SHADOW_WINDOW_ID)
    sync_aws_shadow.add_argument("--skip-unmatched-resources", action="store_true")
    sync_aws_shadow.add_argument("--dry-run", action="store_true")
    sync_aws_shadow.add_argument("--limit", type=int, default=None)

    cutover_aws_split = subparsers.add_parser(
        "cutover-aws-split-cost",
        help="Replace an approved AWS 7266 split-cost usage-date window and refresh attribution.",
    )
    cutover_aws_split.add_argument("--usage-start-date", type=_parse_date, required=True)
    cutover_aws_split.add_argument("--usage-end-date", type=_parse_date, required=True)
    cutover_aws_split.add_argument(
        "--skip-unmatched-resources",
        action="store_true",
        help="Do not sync resource-level unmatched rows for this cutover.",
    )
    cutover_aws_split.add_argument("--dry-run", action="store_true")

    backfill_refine = subparsers.add_parser(
        "backfill-gcp-cost-refine-from-raw",
        help="Backfill cost_bq_export_summary_daily and cost_unmatched_resource_daily from cost_raw_details",
    )
    backfill_refine.add_argument("--start-date", type=_parse_date, required=True)
    backfill_refine.add_argument("--end-date", type=_parse_date, required=True)
    backfill_refine.add_argument(
        "--skip-unmatched-resources",
        action="store_true",
        help="Only backfill cost_bq_export_summary_daily.",
    )
    backfill_refine.add_argument(
        "--mark-summary-watermark",
        action="store_true",
        help="Mark sync-gcp-billing-summary succeeded through the synthetic export partition end.",
    )
    backfill_refine.add_argument("--dry-run", action="store_true")

    refresh_attr = subparsers.add_parser(
        "refresh-cost-attribution-daily",
        help="Rebuild cost_attribution_daily from cost_raw_details and roster tables",
    )
    refresh_attr.add_argument("--start-date", type=_parse_date, required=True)
    refresh_attr.add_argument("--end-date", type=_parse_date, required=True)
    refresh_attr.add_argument("--dry-run", action="store_true")
    refresh_attr.add_argument(
        "--split-by-day",
        action="store_true",
        help="Refresh one usage date at a time; recommended for larger ranges.",
    )

    refresh_summary_attr = subparsers.add_parser(
        "refresh-cost-attribution-from-summary",
        help="Rebuild cost_attribution_daily from cost_bq_export_summary_daily and roster tables",
    )
    refresh_summary_attr.add_argument("--start-date", type=_parse_date, required=True)
    refresh_summary_attr.add_argument("--end-date", type=_parse_date, required=True)
    refresh_summary_attr.add_argument("--dry-run", action="store_true")
    refresh_summary_attr.add_argument(
        "--split-by-day",
        action="store_true",
        help="Refresh one usage date at a time; recommended for larger ranges.",
    )

    sync_gcs_cache = subparsers.add_parser(
        "sync-gcs-cache-last-seen",
        help="Summarize one day of GCS Bazel cache access logs into BigQuery last-seen tables",
    )
    sync_gcs_cache.set_defaults(require_database=False)
    sync_gcs_cache.add_argument("--run-date", type=_parse_date, default=None)
    sync_gcs_cache.add_argument("--dry-run", action="store_true")

    bootstrap_gcs_cache = subparsers.add_parser(
        "bootstrap-gcs-cache-last-seen",
        help="Bootstrap GCS Bazel cache last-seen state from a historical BigQuery log window",
    )
    bootstrap_gcs_cache.set_defaults(require_database=False)
    bootstrap_gcs_cache.add_argument("--start-date", type=_parse_date, required=True)
    bootstrap_gcs_cache.add_argument("--end-date", type=_parse_date, default=None)
    bootstrap_gcs_cache.add_argument("--dry-run", action="store_true")

    sync_gcs_cache_ac_refs = subparsers.add_parser(
        "sync-gcs-cache-ac-references",
        help="Build and refresh the AC to CAS reference index for GCS Bazel cache cleanup",
    )
    sync_gcs_cache_ac_refs.set_defaults(require_database=False)
    sync_gcs_cache_ac_refs.add_argument(
        "--mode",
        choices=("bootstrap", "incremental"),
        required=True,
    )
    sync_gcs_cache_ac_refs.add_argument("--shard-start", type=_parse_non_negative_int, default=0)
    sync_gcs_cache_ac_refs.add_argument("--shard-end", type=_parse_non_negative_int, default=None)
    sync_gcs_cache_ac_refs.add_argument("--dry-run", action="store_true")
    sync_gcs_cache_ac_refs.add_argument(
        "--skip-ensure-tables",
        action="store_true",
        help="Skip AC reference table creation/state initialization for parallel shard workers",
    )

    cleanup_gcs_cache = subparsers.add_parser(
        "cleanup-gcs-cache",
        help="Run CAS-driven cascading cleanup from GCS cache last-seen summaries",
    )
    cleanup_gcs_cache.set_defaults(require_database=False)
    cleanup_gcs_cache.add_argument(
        "--mode",
        choices=("dry-run", "delete"),
        default="dry-run",
    )
    cleanup_gcs_cache.add_argument(
        "--execute-kind",
        choices=("all", "cas", "cas-from-index"),
        default="all",
    )
    cleanup_gcs_cache.add_argument("--ac-retention-days", type=_parse_positive_int, default=None)
    cleanup_gcs_cache.add_argument("--cas-retention-days", type=_parse_positive_int, default=None)
    cleanup_gcs_cache.add_argument("--safety-buffer-days", type=_parse_positive_int, default=None)
    cleanup_gcs_cache.add_argument(
        "--max-delete-objects",
        type=_parse_positive_int,
        default=None,
        help=(
            "Maximum delete target count. For --execute-kind cas-from-index, "
            "this caps CAS candidates; referenced cold ACs are expanded and "
            "deleted first."
        ),
    )
    cleanup_gcs_cache.add_argument(
        "--max-delete-cas-objects",
        type=_parse_positive_int,
        default=None,
        help=(
            "CAS candidate cap used by --execute-kind cas-from-index when "
            "--max-delete-objects is not set."
        ),
    )
    cleanup_gcs_cache.add_argument(
        "--max-delete-ac-objects",
        type=_parse_positive_int,
        default=None,
        help="AC candidate cap for CAS reverse-lookup cleanup.",
    )
    cleanup_gcs_cache.add_argument("--sample-limit", type=_parse_positive_int, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "limit", None) is not None and (
        getattr(args, "replace_existing_dates", False)
        or getattr(args, "replace_existing_partitions", False)
    ):
        raise ValueError("--limit cannot be used with destructive replacement")
    require_database = getattr(args, "require_database", True)
    settings = get_settings(require_database=require_database)
    configure_logging(settings.log_level)

    if args.command == "sync-gcp-billing-export":
        if args.replace_existing_dates and (args.start_date is None or args.end_date is None):
            raise ValueError("--replace-existing-dates requires --start-date and --end-date")
        engine = build_engine(settings)
        try:
            summaries = []
            for gcp_settings in _resolve_gcp_sources(engine, settings=settings.gcp_billing):
                summaries.extend(_run_sync_gcp_command(engine, settings=gcp_settings, args=args))
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-gcp-billing-summary":
        if args.replace_existing_partitions and (
            args.export_partition_start is None or args.export_partition_end is None
        ):
            raise ValueError(
                "--replace-existing-partitions requires --export-partition-start and --export-partition-end"
            )
        engine = build_engine(settings)
        try:
            summaries = []
            for gcp_settings in _resolve_gcp_sources(engine, settings=settings.gcp_billing):
                summaries.append(
                    run_sync_gcp_billing_summary(
                        engine,
                        settings=gcp_settings,
                        export_partition_start=args.export_partition_start,
                        export_partition_end=args.export_partition_end,
                        earliest_usage_date=args.earliest_usage_date,
                        dry_run=args.dry_run,
                        limit=args.limit,
                        replace_existing_partitions=args.replace_existing_partitions,
                    )
                )
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-aws-billing-summary":
        if args.replace_existing_partitions and (
            args.export_partition_start is None or args.export_partition_end is None
        ):
            raise ValueError(
                "--replace-existing-partitions requires --export-partition-start and --export-partition-end"
            )
        if args.replace_existing_dates and (
            args.usage_start_date is None or args.usage_end_date is None
        ):
            raise ValueError("--replace-existing-dates requires --usage-start-date and --usage-end-date")
        engine = build_engine(settings)
        try:
            summaries = []
            sources = _resolve_aws_sources(engine, settings=settings.aws_billing)
            if args.replace_existing_dates and any(
                source.schema_version != AWS_SPLIT_COST_SCHEMA_VERSION for source in sources
            ):
                raise ValueError(
                    "--replace-existing-dates only supports split-cost source profiles; "
                    "use cutover-aws-split-cost for the approved 946646677266 cutover"
                )
            for source in sources:
                summary = run_sync_aws_billing_summary(
                    engine,
                    settings=settings.aws_billing,
                    account_id=source.account_id,
                    export_partition_start=args.export_partition_start,
                    export_partition_end=args.export_partition_end,
                    earliest_usage_date=args.earliest_usage_date,
                    dry_run=args.dry_run,
                    limit=args.limit,
                    replace_existing_partitions=args.replace_existing_partitions,
                    replace_existing_usage_dates=args.replace_existing_dates,
                    usage_start_date=args.usage_start_date,
                    usage_end_date=args.usage_end_date,
                    source=source,
                )
                summaries.append(summary)
                split_usage_dates = tuple(
                    usage_date
                    for usage_date in summary.touched_usage_dates
                    if source.available_from is None or usage_date >= source.available_from
                )
                if source.schema_version == AWS_SPLIT_COST_SCHEMA_VERSION and split_usage_dates:
                    summaries.append(
                        run_sync_aws_parent_residual_allocations(
                            engine,
                            source=source,
                            usage_start_date=min(split_usage_dates),
                            usage_end_date=max(split_usage_dates),
                            export_partition_start=summary.export_partition_start,
                            export_partition_end=summary.export_partition_end,
                            page_size=settings.aws_billing.page_size,
                            dry_run=args.dry_run,
                            validate_guardrail=False,
                        )
                    )
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-gcp-unmatched-resources":
        engine = build_engine(settings)
        try:
            summaries = []
            for gcp_settings in _resolve_gcp_sources(engine, settings=settings.gcp_billing):
                summaries.append(
                    run_sync_gcp_unmatched_resources(
                        engine,
                        settings=gcp_settings,
                        usage_start_date=args.usage_start_date,
                        usage_end_date=args.usage_end_date,
                        export_partition_start=args.export_partition_start,
                        export_partition_end=args.export_partition_end,
                        dry_run=args.dry_run,
                        limit=args.limit,
                    )
                )
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-gcp-kubernetes-workload-allocations":
        engine = build_engine(settings)
        try:
            summaries = []
            for gcp_settings in _resolve_gcp_sources(engine, settings=settings.gcp_billing):
                summaries.append(
                    run_sync_gcp_kubernetes_workload_allocations(
                        engine,
                        settings=gcp_settings,
                        usage_start_date=args.usage_start_date,
                        usage_end_date=args.usage_end_date,
                        export_partition_start=args.export_partition_start,
                        export_partition_end=args.export_partition_end,
                        dry_run=args.dry_run,
                    )
                )
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-aws-unmatched-resources":
        engine = build_engine(settings)
        try:
            summaries = []
            sources = _resolve_aws_sources(engine, settings=settings.aws_billing)
            if args.replace_existing_dates and any(
                source.schema_version != AWS_SPLIT_COST_SCHEMA_VERSION for source in sources
            ):
                raise ValueError(
                    "--replace-existing-dates only supports split-cost source profiles; "
                    "use cutover-aws-split-cost for the approved 946646677266 cutover"
                )
            for source in sources:
                summaries.append(
                    run_sync_aws_unmatched_resources(
                        engine,
                        settings=settings.aws_billing,
                        account_id=source.account_id,
                        usage_start_date=args.usage_start_date,
                        usage_end_date=args.usage_end_date,
                        export_partition_start=args.export_partition_start,
                        export_partition_end=args.export_partition_end,
                        dry_run=args.dry_run,
                        limit=args.limit,
                        replace_existing_usage_dates=args.replace_existing_dates,
                        source=source,
                    )
                )
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "snapshot-aws-split-cost-shadow-legacy":
        engine = build_engine(settings)
        try:
            target = snapshot_aws_split_cost_shadow_legacy(
                engine,
                window_id=args.window_id,
                include_unmatched_resources=not args.skip_unmatched_resources,
                dry_run=args.dry_run,
            )
            print(json.dumps(_summary_to_json(target), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-aws-split-cost-shadow":
        engine = build_engine(settings)
        try:
            summary = run_aws_split_cost_shadow(
                engine,
                settings=settings.aws_billing,
                window_id=args.window_id,
                include_unmatched_resources=not args.skip_unmatched_resources,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            print(json.dumps(_summary_to_json(summary), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "cutover-aws-split-cost":
        if args.usage_start_date.replace(day=1) != args.usage_end_date.replace(day=1):
            raise ValueError(
                "cutover-aws-split-cost only accepts one billing month; "
                "promote cross-month windows separately"
            )
        engine = build_engine(settings)
        try:
            source = _resolve_aws_split_cutover_source(engine, settings=settings.aws_billing)
            export_partition_start = args.usage_start_date.replace(day=1)
            export_partition_end = args.usage_end_date.replace(day=1)
            with _atomic_cutover_engine(engine, dry_run=args.dry_run) as cutover_engine:
                summary = run_sync_aws_billing_summary(
                    cutover_engine,
                    settings=settings.aws_billing,
                    account_id=source.account_id,
                    export_partition_start=export_partition_start,
                    export_partition_end=export_partition_end,
                    earliest_usage_date=args.usage_start_date,
                    dry_run=args.dry_run,
                    limit=None,
                    replace_existing_usage_dates=True,
                    usage_start_date=args.usage_start_date,
                    usage_end_date=args.usage_end_date,
                    validate_guardrail=True,
                    source=source,
                )
                cutover_summaries = [summary]
                if not args.skip_unmatched_resources:
                    cutover_summaries.append(
                        run_sync_aws_unmatched_resources(
                            cutover_engine,
                            settings=settings.aws_billing,
                            account_id=source.account_id,
                            usage_start_date=args.usage_start_date,
                            usage_end_date=args.usage_end_date,
                            export_partition_start=export_partition_start,
                            export_partition_end=export_partition_end,
                            dry_run=args.dry_run,
                            limit=None,
                            replace_existing_usage_dates=True,
                            validate_guardrail=False,
                            source=source,
                        )
                    )
                residual_allocations = run_sync_aws_parent_residual_allocations(
                    cutover_engine,
                    source=source,
                    usage_start_date=args.usage_start_date,
                    usage_end_date=args.usage_end_date,
                    export_partition_start=export_partition_start,
                    export_partition_end=export_partition_end,
                    page_size=settings.aws_billing.page_size,
                    dry_run=args.dry_run,
                    validate_guardrail=False,
                )
                attribution = run_refresh_cost_attribution_from_summary(
                    cutover_engine,
                    source=CostAttributionSource(vendor="aws", account_id=source.account_id),
                    start_date=args.usage_start_date,
                    end_date=args.usage_end_date,
                    dry_run=args.dry_run,
                    tcms_allocation_table=settings.tcms_allocation.allocation_table,
                )
                cutover_summaries.extend((residual_allocations, attribution))
            print(
                json.dumps(
                    _summaries_to_json(cutover_summaries),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            engine.dispose()

    if args.command == "backfill-gcp-cost-refine-from-raw":
        engine = build_engine(settings)
        try:
            summaries = []
            for gcp_settings in _resolve_gcp_sources(engine, settings=settings.gcp_billing):
                summaries.append(
                    run_backfill_cost_refine_from_raw(
                        engine,
                        settings=gcp_settings,
                        start_date=args.start_date,
                        end_date=args.end_date,
                        include_unmatched_resources=not args.skip_unmatched_resources,
                        mark_summary_watermark=args.mark_summary_watermark,
                        dry_run=args.dry_run,
                    )
                )
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "refresh-cost-attribution-daily":
        engine = build_engine(settings)
        try:
            summaries = []
            for source in _resolve_attribution_sources(
                engine,
                gcp_settings=settings.gcp_billing,
                aws_settings=settings.aws_billing,
            ):
                summaries.extend(_run_refresh_attribution_command(engine, source=source, args=args))
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "refresh-cost-attribution-from-summary":
        engine = build_engine(settings)
        try:
            summaries = []
            for source in _resolve_attribution_sources(
                engine,
                gcp_settings=settings.gcp_billing,
                aws_settings=settings.aws_billing,
            ):
                summaries.extend(
                    _run_refresh_attribution_from_summary_command(
                        engine,
                        source=source,
                        args=args,
                        tcms_allocation_table=settings.tcms_allocation.allocation_table,
                    )
                )
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-gcs-cache-last-seen":
        summary = run_sync_gcs_cache_last_seen(
            settings=settings.gcs_cache,
            run_date=args.run_date,
            dry_run=args.dry_run,
        )
        print(json.dumps(_summaries_to_json([summary]), indent=2, sort_keys=True))
        return 0

    if args.command == "bootstrap-gcs-cache-last-seen":
        summary = run_bootstrap_gcs_cache_last_seen(
            settings=settings.gcs_cache,
            start_date=args.start_date,
            end_date=args.end_date,
            dry_run=args.dry_run,
        )
        print(json.dumps(_summaries_to_json([summary]), indent=2, sort_keys=True))
        return 0

    if args.command == "sync-gcs-cache-ac-references":
        summary = run_sync_gcs_cache_ac_references(
            settings=settings.gcs_cache,
            mode=args.mode,
            shard_start=args.shard_start,
            shard_end=args.shard_end,
            dry_run=args.dry_run,
            ensure_tables=not args.skip_ensure_tables,
        )
        print(json.dumps(_summaries_to_json([summary]), indent=2, sort_keys=True))
        return 0

    if args.command == "cleanup-gcs-cache":
        summary = run_cleanup_gcs_cache(
            settings=settings.gcs_cache,
            mode=args.mode,
            execute_kind=args.execute_kind,
            ac_retention_days=args.ac_retention_days,
            cas_retention_days=args.cas_retention_days,
            safety_buffer_days=args.safety_buffer_days,
            max_delete_objects=args.max_delete_objects,
            max_delete_ac_objects=args.max_delete_ac_objects,
            max_delete_cas_objects=args.max_delete_cas_objects,
            sample_limit=args.sample_limit,
        )
        print(json.dumps(_summaries_to_json([summary]), indent=2, sort_keys=True))
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")  # pragma: no cover


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return parsed


def _parse_non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {value!r}")
    return parsed


def _run_sync_gcp_command(engine, *, settings, args):
    logger = logging.getLogger(__name__)
    if args.split_by_day:
        if args.start_date is None or args.end_date is None:
            raise ValueError("--split-by-day requires --start-date and --end-date")
        summaries = []
        for usage_date in _date_range(args.start_date, args.end_date):
            logger.info("sync-gcp-billing-export day started", extra={"usage_date": usage_date})
            summary = run_sync_gcp_billing_export(
                engine,
                settings=settings,
                start_date=usage_date,
                end_date=usage_date,
                dry_run=args.dry_run,
                limit=args.limit,
                replace_existing_dates=args.replace_existing_dates,
            )
            logger.info(
                "sync-gcp-billing-export day finished",
                extra={"summary": summary.__dict__},
            )
            summaries.append(summary)
        return summaries

    summary = run_sync_gcp_billing_export(
        engine,
        settings=settings,
        start_date=args.start_date,
        end_date=args.end_date,
        dry_run=args.dry_run,
        limit=args.limit,
        replace_existing_dates=args.replace_existing_dates,
    )
    logger.info(
        "sync-gcp-billing-export finished",
        extra={"summary": summary.__dict__},
    )
    return [summary]


def _run_refresh_attribution_command(engine, *, source: CostAttributionSource, args):
    logger = logging.getLogger(__name__)
    if args.split_by_day:
        summaries = []
        for usage_date in _date_range(args.start_date, args.end_date):
            logger.info(
                "refresh-cost-attribution-daily day started",
                extra={
                    "vendor": source.vendor,
                    "account_id": source.account_id,
                    "usage_date": usage_date,
                },
            )
            summary = run_refresh_cost_attribution_daily(
                engine,
                source=source,
                start_date=usage_date,
                end_date=usage_date,
                dry_run=args.dry_run,
            )
            logger.info(
                "refresh-cost-attribution-daily day finished",
                extra={"summary": summary.__dict__},
            )
            summaries.append(summary)
        return summaries

    summary = run_refresh_cost_attribution_daily(
        engine,
        source=source,
        start_date=args.start_date,
        end_date=args.end_date,
        dry_run=args.dry_run,
    )
    logger.info(
        "refresh-cost-attribution-daily finished",
        extra={"summary": summary.__dict__},
    )
    return [summary]


def _run_refresh_attribution_from_summary_command(
    engine,
    *,
    source: CostAttributionSource,
    args,
    tcms_allocation_table: str,
):
    logger = logging.getLogger(__name__)
    if args.split_by_day:
        summaries = []
        for usage_date in _date_range(args.start_date, args.end_date):
            logger.info(
                "refresh-cost-attribution-from-summary day started",
                extra={
                    "vendor": source.vendor,
                    "account_id": source.account_id,
                    "usage_date": usage_date,
                },
            )
            summary = run_refresh_cost_attribution_from_summary(
                engine,
                source=source,
                start_date=usage_date,
                end_date=usage_date,
                dry_run=args.dry_run,
                tcms_allocation_table=tcms_allocation_table,
            )
            logger.info(
                "refresh-cost-attribution-from-summary day finished",
                extra={"summary": summary.__dict__},
            )
            summaries.append(summary)
        return summaries

    summary = run_refresh_cost_attribution_from_summary(
        engine,
        source=source,
        start_date=args.start_date,
        end_date=args.end_date,
        dry_run=args.dry_run,
        tcms_allocation_table=tcms_allocation_table,
    )
    logger.info(
        "refresh-cost-attribution-from-summary finished",
        extra={"summary": summary.__dict__},
    )
    return [summary]


def _date_range(start_date: date, end_date: date):
    if start_date > end_date:
        raise ValueError("--start-date must be before or equal to --end-date")
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _resolve_gcp_sources(engine, *, settings: GcpBillingSettings) -> tuple[GcpBillingSettings, ...]:
    sources = _list_sources(engine, vendor="gcp")
    if not sources:
        return (settings,)
    return tuple(replace(settings, account_id=source.account_id) for source in sources)


def _resolve_aws_sources(engine, *, settings: AwsBillingSettings) -> tuple[AwsBillingSource, ...]:
    sources = _list_sources(engine, vendor="aws")
    if sources:
        resolved_sources = []
        for source in sources:
            schema_version = source.source_schema_version or AWS_CUR_LEGACY_SCHEMA_VERSION
            if schema_version == AWS_SPLIT_COST_SCHEMA_VERSION and not source.source_table:
                raise ValueError(
                    f"AWS source profile {source.account_id} uses {AWS_SPLIT_COST_SCHEMA_VERSION} "
                    "but has no source_table"
                )
            resolved_sources.append(
                AwsBillingSource(
                    account_id=source.account_id,
                    billing_table=source.source_table or settings.billing_table,
                    schema_version=schema_version,
                    available_from=source.source_available_from,
                )
            )
        return tuple(resolved_sources)
    if settings.account_id:
        return (
            AwsBillingSource(
                account_id=settings.account_id,
                billing_table=settings.billing_table,
            ),
        )
    logging.getLogger(__name__).warning(
        "No active AWS cost sources found in cost_sources and COST_INSIGHT_AWS_ACCOUNT_ID is not set."
    )
    return ()


def _resolve_aws_split_cutover_source(
    engine,
    *,
    settings: AwsBillingSettings,
) -> AwsBillingSource:
    sources = _resolve_aws_sources(engine, settings=settings)
    matches = [
        source
        for source in sources
        if source.account_id == AWS_7266_ACCOUNT_ID
        and source.schema_version == AWS_SPLIT_COST_SCHEMA_VERSION
        and source.available_from is not None
    ]
    if len(matches) != 1:
        raise ValueError(
            "cutover-aws-split-cost requires one active AWS 946646677266 "
            "source profile with aws_split_cost_v1 and source_available_from"
        )
    return matches[0]


def _resolve_attribution_sources(
    engine,
    *,
    gcp_settings: GcpBillingSettings,
    aws_settings: AwsBillingSettings,
) -> tuple[CostAttributionSource, ...]:
    sources = _list_sources(engine, vendor=None)
    if sources:
        return tuple(
            CostAttributionSource(vendor=source.vendor, account_id=source.account_id)
            for source in sources
        )
    fallback_sources = [CostAttributionSource(vendor="gcp", account_id=gcp_settings.account_id)]
    if aws_settings.account_id:
        fallback_sources.append(
            CostAttributionSource(vendor="aws", account_id=aws_settings.account_id)
        )
    return tuple(fallback_sources)


def _list_sources(engine, *, vendor: str | None):
    if not hasattr(engine, "begin"):
        return ()
    with engine.begin() as connection:
        return list_active_cost_sources(connection, vendor=vendor)


def _summaries_to_json(summaries: Sequence[object]) -> object:
    payload = [_summary_to_json(summary) for summary in summaries]
    return payload[0] if len(payload) == 1 else payload


def _summary_to_json(summary) -> dict[str, object]:
    return {key: _jsonable(value) for key, value in vars(summary).items() if value is not None}


def _jsonable(value):
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return {key: _jsonable(item) for key, item in vars(value).items() if item is not None}
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
