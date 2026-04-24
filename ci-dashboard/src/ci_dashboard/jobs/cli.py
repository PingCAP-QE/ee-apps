from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timedelta

from ci_dashboard.common.config import get_settings
from ci_dashboard.common.db import build_engine
from ci_dashboard.common.logging import configure_logging
from ci_dashboard.jobs.refresh_build_derived import (
    run_refresh_build_derived,
    run_refresh_build_derived_for_time_window,
    run_refresh_flaky_signals_for_time_window,
)
from ci_dashboard.jobs.sync_builds import run_sync_builds, run_sync_builds_for_time_window
from ci_dashboard.jobs.sync_pr_events import (
    run_sync_pr_events,
    run_sync_pr_events_for_time_window,
)
from ci_dashboard.jobs.sync_flaky_issues import run_sync_flaky_issues
from ci_dashboard.jobs.sync_pods import run_reconcile_pod_linkage_for_time_window, run_sync_pods


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CI dashboard job runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync-builds", help="Sync prow_jobs into ci_l1_builds")
    subparsers.add_parser("sync-pr-events", help="Sync github_tickets into ci_l1_pr_events")
    subparsers.add_parser(
        "sync-flaky-issues",
        help="Sync flaky GitHub issues into ci_l1_flaky_issues",
    )
    subparsers.add_parser(
        "sync-pods",
        help="Sync Kubernetes pod lifecycle events into ci_l1_pod_* tables",
    )
    subparsers.add_parser(
        "refresh-build-derived",
        help="Refresh derived fields on ci_l1_builds",
    )
    refresh_range_parser = subparsers.add_parser(
        "refresh-build-derived-range",
        help="Refresh derived build fields for an inclusive build date range without touching incremental job watermarks",
    )
    refresh_range_parser.add_argument(
        "--start-date",
        required=True,
        type=_parse_iso_date,
        help="Inclusive build date in YYYY-MM-DD",
    )
    refresh_range_parser.add_argument(
        "--end-date",
        type=_parse_iso_date,
        help="Inclusive build date in YYYY-MM-DD; defaults to open-ended",
    )
    flaky_refresh_range_parser = subparsers.add_parser(
        "refresh-flaky-signals-range",
        help="Refresh only flaky/retry-loop signals and failure categories for an inclusive build date range",
    )
    flaky_refresh_range_parser.add_argument(
        "--start-date",
        required=True,
        type=_parse_iso_date,
        help="Inclusive build date in YYYY-MM-DD",
    )
    flaky_refresh_range_parser.add_argument(
        "--end-date",
        type=_parse_iso_date,
        help="Inclusive build date in YYYY-MM-DD; defaults to open-ended",
    )
    pod_link_reconcile_range_parser = subparsers.add_parser(
        "reconcile-pod-linkage-range",
        help="Reconcile pod lifecycle rows against build rows for an inclusive scheduled date range",
    )
    pod_link_reconcile_range_parser.add_argument(
        "--start-date",
        required=True,
        type=_parse_iso_date,
        help="Inclusive pod scheduled date in YYYY-MM-DD",
    )
    pod_link_reconcile_range_parser.add_argument(
        "--end-date",
        type=_parse_iso_date,
        help="Inclusive pod scheduled date in YYYY-MM-DD; defaults to open-ended",
    )
    backfill_parser = subparsers.add_parser(
        "backfill-range",
        help="Idempotently re-import a build date range without touching incremental job watermarks",
    )
    backfill_parser.add_argument(
        "--start-date",
        required=True,
        type=_parse_iso_date,
        help="Inclusive build date in YYYY-MM-DD",
    )
    backfill_parser.add_argument(
        "--end-date",
        type=_parse_iso_date,
        help="Inclusive build date in YYYY-MM-DD; defaults to open-ended",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)

    if args.command == "sync-builds":
        engine = build_engine(settings)
        summary = run_sync_builds(engine, settings)
        logging.getLogger(__name__).info("sync-builds finished", extra={"summary": summary.__dict__})
        return 0

    if args.command == "sync-pr-events":
        engine = build_engine(settings)
        summary = run_sync_pr_events(engine, settings)
        logging.getLogger(__name__).info("sync-pr-events finished", extra={"summary": summary.__dict__})
        return 0

    if args.command == "sync-flaky-issues":
        engine = build_engine(settings)
        summary = run_sync_flaky_issues(engine, settings)
        logging.getLogger(__name__).info(
            "sync-flaky-issues finished",
            extra={"summary": summary.__dict__},
        )
        return 0

    if args.command == "sync-pods":
        engine = build_engine(settings)
        summary = run_sync_pods(engine, settings)
        logging.getLogger(__name__).info(
            "sync-pods finished",
            extra={"summary": summary.__dict__},
        )
        return 0

    if args.command == "refresh-build-derived":
        engine = build_engine(settings)
        summary = run_refresh_build_derived(engine, settings)
        logging.getLogger(__name__).info(
            "refresh-build-derived finished",
            extra={"summary": summary.__dict__},
        )
        return 0

    if args.command == "refresh-build-derived-range":
        if args.end_date is not None and args.start_date > args.end_date:
            parser.error("--start-date must be on or before --end-date")

        start_time_from, start_time_to = _build_time_window(
            start_date=args.start_date,
            end_date=args.end_date,
        )
        engine = build_engine(settings)
        summary = run_refresh_build_derived_for_time_window(
            engine,
            settings,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
        )
        logging.getLogger(__name__).info(
            "refresh-build-derived-range finished",
            extra={
                "start_time_from": start_time_from.isoformat(sep=" "),
                "start_time_to": start_time_to.isoformat(sep=" ") if start_time_to else None,
                "summary": summary.__dict__,
            },
        )
        return 0

    if args.command == "refresh-flaky-signals-range":
        if args.end_date is not None and args.start_date > args.end_date:
            parser.error("--start-date must be on or before --end-date")

        start_time_from, start_time_to = _build_time_window(
            start_date=args.start_date,
            end_date=args.end_date,
        )
        engine = build_engine(settings)
        summary = run_refresh_flaky_signals_for_time_window(
            engine,
            settings,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
        )
        logging.getLogger(__name__).info(
            "refresh-flaky-signals-range finished",
            extra={
                "start_time_from": start_time_from.isoformat(sep=" "),
                "start_time_to": start_time_to.isoformat(sep=" ") if start_time_to else None,
                "summary": summary.__dict__,
            },
        )
        return 0

    if args.command == "reconcile-pod-linkage-range":
        if args.end_date is not None and args.start_date > args.end_date:
            parser.error("--start-date must be on or before --end-date")

        start_time_from, start_time_to = _build_time_window(
            start_date=args.start_date,
            end_date=args.end_date,
        )
        engine = build_engine(settings)
        summary = run_reconcile_pod_linkage_for_time_window(
            engine,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
        )
        logging.getLogger(__name__).info(
            "reconcile-pod-linkage-range finished",
            extra={
                "start_time_from": start_time_from.isoformat(sep=" "),
                "start_time_to": start_time_to.isoformat(sep=" ") if start_time_to else None,
                "summary": summary.__dict__,
            },
        )
        return 0

    if args.command == "backfill-range":
        if args.end_date is not None and args.start_date > args.end_date:
            parser.error("--start-date must be on or before --end-date")

        start_time_from, start_time_to = _build_time_window(
            start_date=args.start_date,
            end_date=args.end_date,
        )
        engine = build_engine(settings)
        builds_summary = run_sync_builds_for_time_window(
            engine,
            settings,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
        )
        pr_events_summary = run_sync_pr_events_for_time_window(
            engine,
            settings,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
        )
        refresh_summary = run_refresh_build_derived_for_time_window(
            engine,
            settings,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
        )
        logging.getLogger(__name__).info(
            "backfill-range finished",
            extra={
                "start_time_from": start_time_from.isoformat(sep=" "),
                "start_time_to": start_time_to.isoformat(sep=" ") if start_time_to else None,
                "builds_summary": builds_summary.__dict__,
                "pr_events_summary": pr_events_summary.__dict__,
                "refresh_summary": refresh_summary.__dict__,
            },
        )
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


def _build_time_window(*, start_date: date, end_date: date | None) -> tuple[datetime, datetime | None]:
    start_time_from = datetime.combine(start_date, time.min)
    if end_date is None:
        return start_time_from, None
    return start_time_from, datetime.combine(end_date + timedelta(days=1), time.min)


if __name__ == "__main__":
    raise SystemExit(main())
