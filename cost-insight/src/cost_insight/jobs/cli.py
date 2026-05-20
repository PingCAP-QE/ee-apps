from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from datetime import date, timedelta

from cost_insight.common.config import get_settings
from cost_insight.common.db import build_engine
from cost_insight.common.logging import configure_logging
from cost_insight.jobs.refresh_attribution_daily import run_refresh_cost_attribution_daily
from cost_insight.jobs.sync_gcp_billing_export import run_sync_gcp_billing_export


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
        "start_date": summary.start_date.isoformat(),
        "end_date": summary.end_date.isoformat(),
        "dry_run": summary.dry_run,
    }
    for field in ("rows_seen", "rows_written", "rows_deleted", "rows_inserted", "raw_rows"):
        if hasattr(summary, field) and getattr(summary, field) is not None:
            payload[field] = getattr(summary, field)
    return payload


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
