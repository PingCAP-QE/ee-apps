from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import date, timedelta

from cost_insight.common.config import get_settings
from cost_insight.common.db import build_engine
from cost_insight.common.logging import configure_logging
from cost_insight.jobs.backfill_cost_refine_from_raw import run_backfill_cost_refine_from_raw
from cost_insight.jobs.refresh_attribution_daily import (
    run_refresh_cost_attribution_daily,
    run_refresh_cost_attribution_from_summary,
)
from cost_insight.jobs.sync_gcp_billing_summary import run_sync_gcp_billing_summary
from cost_insight.jobs.sync_gcp_billing_export import run_sync_gcp_billing_export
from cost_insight.jobs.sync_gcp_unmatched_resources import run_sync_gcp_unmatched_resources


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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings(require_database=True)
    configure_logging(settings.log_level)

    if args.command == "sync-gcp-billing-export":
        engine = build_engine(settings)
        try:
            summaries = _run_sync_gcp_command(engine, settings=settings.gcp_billing, args=args)
            payload = (
                [_summary_to_json(summary) for summary in summaries]
                if args.split_by_day
                else _summary_to_json(summaries[0])
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-gcp-billing-summary":
        engine = build_engine(settings)
        try:
            summary = run_sync_gcp_billing_summary(
                engine,
                settings=settings.gcp_billing,
                export_partition_start=args.export_partition_start,
                export_partition_end=args.export_partition_end,
                earliest_usage_date=args.earliest_usage_date,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            print(json.dumps(_summary_to_json(summary), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "sync-gcp-unmatched-resources":
        engine = build_engine(settings)
        try:
            summary = run_sync_gcp_unmatched_resources(
                engine,
                settings=settings.gcp_billing,
                usage_start_date=args.usage_start_date,
                usage_end_date=args.usage_end_date,
                export_partition_start=args.export_partition_start,
                export_partition_end=args.export_partition_end,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            print(json.dumps(_summary_to_json(summary), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "backfill-gcp-cost-refine-from-raw":
        engine = build_engine(settings)
        try:
            summary = run_backfill_cost_refine_from_raw(
                engine,
                settings=settings.gcp_billing,
                start_date=args.start_date,
                end_date=args.end_date,
                include_unmatched_resources=not args.skip_unmatched_resources,
                mark_summary_watermark=args.mark_summary_watermark,
                dry_run=args.dry_run,
            )
            print(json.dumps(_summary_to_json(summary), indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "refresh-cost-attribution-daily":
        engine = build_engine(settings)
        try:
            summaries = _run_refresh_attribution_command(
                engine,
                settings=settings.gcp_billing,
                args=args,
            )
            payload = (
                [_summary_to_json(summary) for summary in summaries]
                if args.split_by_day
                else _summary_to_json(summaries[0])
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    if args.command == "refresh-cost-attribution-from-summary":
        engine = build_engine(settings)
        try:
            summaries = _run_refresh_attribution_from_summary_command(
                engine,
                settings=settings.gcp_billing,
                args=args,
            )
            payload = (
                [_summary_to_json(summary) for summary in summaries]
                if args.split_by_day
                else _summary_to_json(summaries[0])
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        finally:
            engine.dispose()

    raise AssertionError(f"Unhandled command: {args.command}")  # pragma: no cover


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


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


def _run_refresh_attribution_command(engine, *, settings, args):
    logger = logging.getLogger(__name__)
    if args.split_by_day:
        summaries = []
        for usage_date in _date_range(args.start_date, args.end_date):
            logger.info(
                "refresh-cost-attribution-daily day started",
                extra={"usage_date": usage_date},
            )
            summary = run_refresh_cost_attribution_daily(
                engine,
                settings=settings,
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
        settings=settings,
        start_date=args.start_date,
        end_date=args.end_date,
        dry_run=args.dry_run,
    )
    logger.info(
        "refresh-cost-attribution-daily finished",
        extra={"summary": summary.__dict__},
    )
    return [summary]


def _run_refresh_attribution_from_summary_command(engine, *, settings, args):
    logger = logging.getLogger(__name__)
    if args.split_by_day:
        summaries = []
        for usage_date in _date_range(args.start_date, args.end_date):
            logger.info(
                "refresh-cost-attribution-from-summary day started",
                extra={"usage_date": usage_date},
            )
            summary = run_refresh_cost_attribution_from_summary(
                engine,
                settings=settings,
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
        settings=settings,
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


def _summary_to_json(summary) -> dict[str, object]:
    payload = {
        "account_id": summary.account_id,
        "dry_run": summary.dry_run,
    }
    for field in (
        "start_date",
        "end_date",
        "usage_start_date",
        "usage_end_date",
        "export_partition_start",
        "export_partition_end",
    ):
        if hasattr(summary, field) and getattr(summary, field) is not None:
            payload[field] = getattr(summary, field).isoformat()
    for field in (
        "rows_seen",
        "rows_written",
        "rows_deleted",
        "rows_inserted",
        "raw_rows",
        "summary_rows",
        "summary_rows_seen",
        "summary_rows_written",
        "unmatched_rows_seen",
        "unmatched_rows_written",
    ):
        if hasattr(summary, field) and getattr(summary, field) is not None:
            payload[field] = getattr(summary, field)
    if hasattr(summary, "marked_summary_watermark"):
        payload["marked_summary_watermark"] = summary.marked_summary_watermark
    if hasattr(summary, "touched_usage_dates"):
        payload["touched_usage_dates"] = [
            usage_date.isoformat() for usage_date in summary.touched_usage_dates
        ]
    return payload


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
