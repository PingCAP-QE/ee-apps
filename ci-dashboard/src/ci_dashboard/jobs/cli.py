from __future__ import annotations

import argparse
import logging
from datetime import UTC, date, datetime, time, timedelta

from ci_dashboard.common.config import get_settings
from ci_dashboard.common.db import build_engine
from ci_dashboard.common.logging import configure_logging
from ci_dashboard.jobs.analyze_errors import review_error_classification, run_analyze_errors
from ci_dashboard.jobs.archive_error_logs import run_archive_error_logs
from ci_dashboard.jobs.refresh_build_derived import (
    run_refresh_build_derived,
    run_refresh_build_derived_for_time_window,
    run_refresh_flaky_signals_for_time_window,
)
from ci_dashboard.jobs.repair_job_names import run_repair_job_names
from ci_dashboard.jobs.jenkins_worker import run_consume_jenkins_events
from ci_dashboard.jobs.jenkins_timings import run_backfill_jenkins_timings
from ci_dashboard.jobs.sync_builds import run_sync_builds, run_sync_builds_for_time_window
from ci_dashboard.jobs.sync_pr_events import (
    run_sync_pr_events,
    run_sync_pr_events_for_time_window,
)
from ci_dashboard.jobs.sync_flaky_issues import (
    run_backfill_flaky_issue_pr_links,
    run_sync_flaky_issues,
)
from ci_dashboard.jobs.sync_unattached_ebs_volumes import (
    run_sync_unattached_block_volumes,
    run_sync_unattached_ebs_volumes,
)
from ci_dashboard.jobs.check_data_freshness import run_check_data_freshness
from ci_dashboard.jobs.pod_source import cloud_phase_for_source
from ci_dashboard.jobs.pod_watcher import run_watch_pods
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
    consume_jenkins_parser = subparsers.add_parser(
        "consume-jenkins-events",
        help="Consume Jenkins finished events from Kafka into ci_l1_builds",
    )
    consume_jenkins_parser.add_argument(
        "--max-messages",
        type=int,
        help="Stop after processing at most this many Kafka messages",
    )
    consume_jenkins_parser.add_argument(
        "--group-id",
        help="Override the Kafka consumer group id for this run",
    )
    consume_jenkins_parser.add_argument(
        "--topic",
        help="Override the Kafka topic name for this run",
    )
    backfill_jenkins_timings_parser = subparsers.add_parser(
        "backfill-jenkins-timings",
        help="Best-effort backfill of Jenkins timings for recent completed builds",
    )
    backfill_jenkins_timings_parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Completed-build lookback window in days. Default: 30",
    )
    backfill_jenkins_timings_parser.add_argument(
        "--limit",
        type=int,
        help="Process at most this many builds",
    )
    subparsers.add_parser("sync-pr-events", help="Sync github_tickets into ci_l1_pr_events")
    subparsers.add_parser(
        "sync-flaky-issues",
        help="Sync flaky GitHub issues into ci_l1_flaky_issues",
    )
    subparsers.add_parser(
        "backfill-flaky-issue-pr-links",
        help="Rebuild ci_l1_flaky_issue_pr_links from github_tickets flaky issue timelines",
    )
    subparsers.add_parser(
        "sync-pods",
        help="Sync Kubernetes pod lifecycle events into ci_l1_pod_* tables",
    )
    watch_pods_parser = subparsers.add_parser(
        "watch-pods",
        help="Watch live Kubernetes pods/events into ci_l1_pod_* tables",
    )
    watch_pods_parser.add_argument(
        "--max-events",
        type=int,
        help="Stop after observing this many pod snapshots or Kubernetes events",
    )
    subparsers.add_parser(
        "repair-job-names",
        help="Backfill short job_name values in ci_l1_builds and ci_l1_pod_lifecycle to full repo/job form",
    )
    subparsers.add_parser(
        "refresh-build-derived",
        help="Refresh derived fields on ci_l1_builds",
    )
    archive_logs_parser = subparsers.add_parser(
        "archive-error-logs",
        help="Archive redacted Jenkins error console tails to GCS",
    )
    archive_logs_parser.add_argument(
        "--limit",
        type=int,
        help="Archive at most this many candidate builds",
    )
    archive_logs_parser.add_argument(
        "--build-id",
        type=int,
        help="Archive a specific ci_l1_builds row id",
    )
    archive_logs_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing archived log for the selected build(s)",
    )
    analyze_errors_parser = subparsers.add_parser(
        "analyze-errors",
        help="Classify archived Jenkins error logs",
    )
    analyze_errors_parser.add_argument(
        "--limit",
        type=int,
        help="Classify at most this many candidate builds",
    )
    analyze_errors_parser.add_argument(
        "--build-id",
        type=int,
        help="Classify a specific ci_l1_builds row id",
    )
    analyze_errors_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing machine classification for the selected build(s)",
    )
    review_error_parser = subparsers.add_parser(
        "review-error",
        help="Apply a human review classification to a build row",
    )
    review_error_parser.add_argument(
        "--build-id",
        required=True,
        type=int,
        help="Target ci_l1_builds row id",
    )
    review_error_parser.add_argument(
        "--l1",
        required=True,
        help="Human-reviewed L1 category",
    )
    review_error_parser.add_argument(
        "--l2",
        required=True,
        help="Human-reviewed L2 subcategory",
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
        help="Reconcile saved pod linkage in a bounded inclusive UTC date range (DB only)",
    )
    pod_link_reconcile_range_parser.add_argument(
        "--start-date",
        required=True,
        type=_parse_iso_date,
        help="Inclusive pod scheduled/creation date in YYYY-MM-DD",
    )
    pod_link_reconcile_range_parser.add_argument(
        "--end-date",
        type=_parse_iso_date,
        help="Inclusive pod scheduled/creation date in YYYY-MM-DD; defaults to today's UTC date",
    )
    pod_link_reconcile_range_parser.add_argument("--source-project", help="Exact saved collector source identity")
    pod_link_reconcile_range_parser.add_argument("--batch-size", type=int, default=1000, help="Rows per committed batch (1..1000)")
    pod_link_reconcile_range_parser.add_argument("--max-rows", type=int, default=10000, help="Maximum rows scanned (1..10000)")
    pod_link_reconcile_range_parser.add_argument("--after-id", type=int, default=0, help="Resume after the last committed lifecycle ID")
    pod_link_reconcile_range_parser.add_argument("--through-id", type=int, help="Frozen lifecycle ID ceiling from the preceding run")
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
    subparsers.add_parser(
        "check-data-freshness",
        help="Daily data freshness check and Lark alert",
    )
    # Keep this AWS-only entrypoint for manual backfills; production schedules should use
    # sync-unattached-block-volumes so AWS and GCP snapshots advance together.
    subparsers.add_parser(
        "sync-unattached-ebs-volumes",
        help="Debug/backfill only: scan AWS available EBS volumes without GCP",
    )
    subparsers.add_parser(
        "sync-unattached-block-volumes",
        help="Scan AWS EBS and GCP block volumes into the cost-save snapshot table",
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

    if args.command == "consume-jenkins-events":
        if args.max_messages is not None and args.max_messages <= 0:
            parser.error("--max-messages must be positive")
        engine = build_engine(settings)
        summary = run_consume_jenkins_events(
            engine,
            settings,
            max_messages=args.max_messages,
            topic=args.topic,
            group_id=args.group_id,
        )
        logging.getLogger(__name__).info(
            "consume-jenkins-events finished",
            extra={"summary": summary.__dict__},
        )
        return 0

    if args.command == "backfill-jenkins-timings":
        if args.lookback_days <= 0:
            parser.error("--lookback-days must be positive")
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        engine = build_engine(settings)
        summary = run_backfill_jenkins_timings(
            engine,
            settings,
            lookback_days=args.lookback_days,
            limit=args.limit,
        )
        logging.getLogger(__name__).info(
            "backfill-jenkins-timings finished",
            extra={"summary": summary.__dict__},
        )
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

    if args.command == "backfill-flaky-issue-pr-links":
        engine = build_engine(settings)
        summary = run_backfill_flaky_issue_pr_links(engine, settings)
        logging.getLogger(__name__).info(
            "backfill-flaky-issue-pr-links finished",
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

    if args.command == "watch-pods":
        if args.max_events is not None and args.max_events <= 0:
            parser.error("--max-events must be positive")
        engine = build_engine(settings)
        summary = run_watch_pods(engine, settings, max_events=args.max_events)
        logging.getLogger(__name__).info(
            "watch-pods finished",
            extra={"summary": summary.__dict__},
        )
        return 0

    if args.command == "repair-job-names":
        engine = build_engine(settings)
        summary = run_repair_job_names(engine)
        logging.getLogger(__name__).info(
            "repair-job-names finished",
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

    if args.command == "archive-error-logs":
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        if args.build_id is not None and args.build_id <= 0:
            parser.error("--build-id must be positive")
        engine = build_engine(settings)
        summary = run_archive_error_logs(
            engine,
            settings,
            limit=args.limit,
            build_id=args.build_id,
            force=args.force,
        )
        logging.getLogger(__name__).info(
            "archive-error-logs finished",
            extra={"summary": summary.__dict__},
        )
        return 0

    if args.command == "analyze-errors":
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        if args.build_id is not None and args.build_id <= 0:
            parser.error("--build-id must be positive")
        engine = build_engine(settings)
        summary = run_analyze_errors(
            engine,
            settings,
            limit=args.limit,
            build_id=args.build_id,
            force=args.force,
        )
        logging.getLogger(__name__).info(
            "analyze-errors finished",
            extra={"summary": summary.__dict__},
        )
        return 0

    if args.command == "review-error":
        if args.build_id <= 0:
            parser.error("--build-id must be positive")
        engine = build_engine(settings)
        try:
            summary = review_error_classification(
                engine,
                build_id=args.build_id,
                l1_category=args.l1,
                l2_subcategory=args.l2,
            )
        except ValueError as exc:
            parser.error(str(exc))
        logging.getLogger(__name__).info(
            "review-error finished",
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
        end_date = args.end_date or datetime.now(UTC).date()
        if args.start_date > end_date:
            parser.error("--start-date must be on or before --end-date (defaults to today's UTC date)")
        if not 1 <= args.batch_size <= 1000 or not 1 <= args.max_rows <= 10000:
            parser.error("--batch-size must be 1..1000 and --max-rows must be 1..10000")
        if args.after_id < 0 or (args.through_id is not None and args.through_id < args.after_id):
            parser.error("require 0 <= --after-id <= --through-id")
        if args.source_project is not None:
            try:
                cloud_phase_for_source(args.source_project)
            except ValueError as exc:
                parser.error(str(exc))
        start_time_from, start_time_to = _build_time_window(
            start_date=args.start_date,
            end_date=end_date,
        )
        logging.getLogger(__name__).info(
            "reconcile-pod-linkage-range window resolved",
            extra={
                "end_date": end_date.isoformat(),
                "source_project": args.source_project,
                "start_time_from": start_time_from.isoformat(sep=" "),
                "start_time_to": start_time_to.isoformat(sep=" "),
            },
        )
        engine = build_engine(settings)
        summary = run_reconcile_pod_linkage_for_time_window(
            engine,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
            source_project=args.source_project,
            batch_size=args.batch_size,
            max_rows=args.max_rows,
            after_id=args.after_id,
            through_id=args.through_id,
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

    if args.command == "check-data-freshness":
        report = run_check_data_freshness(settings)
        exit_code = 0 if report.passed_all else 1
        logging.getLogger(__name__).info(
            "check-data-freshness finished",
            extra={
                "passed": len(report.results) - len(report.failed),
                "failed": len(report.failed),
                "total": len(report.results),
            },
        )
        return exit_code

    if args.command == "sync-unattached-ebs-volumes":
        engine = build_engine(settings)
        summary = run_sync_unattached_ebs_volumes(engine, settings)
        logging.getLogger(__name__).info(
            "sync-unattached-ebs-volumes finished",
            extra={"summary": summary.__dict__},
        )
        return 0

    if args.command == "sync-unattached-block-volumes":
        engine = build_engine(settings)
        summary = run_sync_unattached_block_volumes(engine, settings)
        logging.getLogger(__name__).info(
            "sync-unattached-block-volumes finished",
            extra={"summary": summary.__dict__},
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
