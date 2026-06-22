from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta

from cost_insight.common.config import AwsBillingSettings, GcpBillingSettings, get_settings
from cost_insight.common.db import build_engine
from cost_insight.common.logging import configure_logging
from cost_insight.jobs.backfill_cost_refine_from_raw import run_backfill_cost_refine_from_raw
from cost_insight.jobs.bootstrap_gcs_cache_last_seen import run_bootstrap_gcs_cache_last_seen
from cost_insight.jobs.cleanup_gcs_cache import run_cleanup_gcs_cache
from cost_insight.jobs.cost_sources import list_active_cost_sources
from cost_insight.jobs.refresh_attribution_daily import (
    CostAttributionSource,
    run_refresh_cost_attribution_daily,
    run_refresh_cost_attribution_from_summary,
)
from cost_insight.jobs.sync_gcs_cache_last_seen import run_sync_gcs_cache_last_seen
from cost_insight.jobs.sync_aws_billing_summary import run_sync_aws_billing_summary
from cost_insight.jobs.sync_aws_unmatched_resources import run_sync_aws_unmatched_resources
from cost_insight.jobs.sync_gcp_billing_summary import run_sync_gcp_billing_summary
from cost_insight.jobs.sync_gcp_billing_export import run_sync_gcp_billing_export
from cost_insight.jobs.sync_gcp_unmatched_resources import run_sync_gcp_unmatched_resources
from cost_insight.jobs.sync_gcs_cache_ac_references import run_sync_gcs_cache_ac_references


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

    sync_aws_summary = subparsers.add_parser(
        "sync-aws-billing-summary",
        help="Sync AWS billing export partitions into cost_bq_export_summary_daily",
    )
    sync_aws_summary.add_argument("--export-partition-start", type=_parse_date, default=None)
    sync_aws_summary.add_argument("--export-partition-end", type=_parse_date, default=None)
    sync_aws_summary.add_argument("--earliest-usage-date", type=_parse_date, default=None)
    sync_aws_summary.add_argument("--dry-run", action="store_true")
    sync_aws_summary.add_argument("--limit", type=int, default=None)

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
        choices=("all", "cas"),
        default="all",
    )
    cleanup_gcs_cache.add_argument("--cas-retention-days", type=_parse_positive_int, default=None)
    cleanup_gcs_cache.add_argument("--safety-buffer-days", type=_parse_positive_int, default=None)
    cleanup_gcs_cache.add_argument("--max-delete-objects", type=_parse_positive_int, default=None)
    cleanup_gcs_cache.add_argument("--sample-limit", type=_parse_positive_int, default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    require_database = getattr(args, "require_database", True)
    settings = get_settings(require_database=require_database)
    configure_logging(settings.log_level)

    if args.command == "sync-gcp-billing-export":
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
                    )
                )
            print(json.dumps(_summaries_to_json(summaries), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-aws-billing-summary":
        engine = build_engine(settings)
        try:
            summaries = []
            for account_id in _resolve_aws_sources(engine, settings=settings.aws_billing):
                summaries.append(
                    run_sync_aws_billing_summary(
                        engine,
                        settings=settings.aws_billing,
                        account_id=account_id,
                        export_partition_start=args.export_partition_start,
                        export_partition_end=args.export_partition_end,
                        earliest_usage_date=args.earliest_usage_date,
                        dry_run=args.dry_run,
                        limit=args.limit,
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

    if args.command == "sync-aws-unmatched-resources":
        engine = build_engine(settings)
        try:
            summaries = []
            for account_id in _resolve_aws_sources(engine, settings=settings.aws_billing):
                summaries.append(
                    run_sync_aws_unmatched_resources(
                        engine,
                        settings=settings.aws_billing,
                        account_id=account_id,
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
                    _run_refresh_attribution_from_summary_command(engine, source=source, args=args)
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
        )
        print(json.dumps(_summaries_to_json([summary]), indent=2, sort_keys=True))
        return 0

    if args.command == "cleanup-gcs-cache":
        summary = run_cleanup_gcs_cache(
            settings=settings.gcs_cache,
            mode=args.mode,
            execute_kind=args.execute_kind,
            cas_retention_days=args.cas_retention_days,
            safety_buffer_days=args.safety_buffer_days,
            max_delete_objects=args.max_delete_objects,
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
                extra={"vendor": source.vendor, "account_id": source.account_id, "usage_date": usage_date},
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


def _run_refresh_attribution_from_summary_command(engine, *, source: CostAttributionSource, args):
    logger = logging.getLogger(__name__)
    if args.split_by_day:
        summaries = []
        for usage_date in _date_range(args.start_date, args.end_date):
            logger.info(
                "refresh-cost-attribution-from-summary day started",
                extra={"vendor": source.vendor, "account_id": source.account_id, "usage_date": usage_date},
            )
            summary = run_refresh_cost_attribution_from_summary(
                engine,
                source=source,
                start_date=usage_date,
                end_date=usage_date,
                dry_run=args.dry_run,
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


def _resolve_aws_sources(engine, *, settings: AwsBillingSettings) -> tuple[str, ...]:
    sources = _list_sources(engine, vendor="aws")
    if sources:
        return tuple(source.account_id for source in sources)
    if settings.account_id:
        return (settings.account_id,)
    logging.getLogger(__name__).warning(
        "No active AWS cost sources found in cost_sources and COST_INSIGHT_AWS_ACCOUNT_ID is not set."
    )
    return ()


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
    return {
        key: _jsonable(value)
        for key, value in vars(summary).items()
        if value is not None
    }


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
        return {
            key: _jsonable(item)
            for key, item in vars(value).items()
            if item is not None
        }
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
