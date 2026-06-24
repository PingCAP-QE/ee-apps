"""Daily data freshness check for ci-dashboard and cost-insight core tables.

Runs a battery of SQL queries against the ci-dashboard and cost-insight
databases, compares each table's latest timestamp against a configured
lag threshold, and sends a Lark incoming-webhook alert for any violations.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.db import build_engine, install_sqlite_functions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Check definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """A single freshness check against a database table or job_state row."""

    name: str  # human-readable label, e.g. "ci_l1_builds"
    level: str  # "HIGH" | "MEDIUM" | "LOW"
    description: str
    # Threshold: if the observed lag exceeds this, the check fails.
    threshold_description: str  # e.g. "4 hours"
    # SQL that returns ONE ROW with a single column: the latest timestamp (or count).
    sql: str
    # Which DB engine to use: "ci" | "cost"
    db: str = "ci"
    # If True, the SQL returns a COUNT rather than a timestamp — check value > 0 → fail.
    is_count_check: bool = False
    # Optional: override the formatter for the failure message.
    # Receives the raw query result value.
    format_failure: Callable[[Any], str] | None = None


# ---------------------------------------------------------------------------
# Check definitions — one per monitored table / pipeline
# ---------------------------------------------------------------------------

CHECKS: list[Check] = [
    # --- CI Dashboard tables ---
    Check(
        name="ci_l1_builds",
        level="HIGH",
        description="Latest build start_time in ci_l1_builds",
        threshold_description="4 hours",
        sql="SELECT MAX(start_time) FROM ci_l1_builds WHERE start_time IS NOT NULL",
        db="ci",
    ),
    Check(
        name="ci_l1_pod_lifecycle",
        level="HIGH",
        description="Latest pod event time in ci_l1_pod_lifecycle",
        threshold_description="4 hours",
        sql="SELECT MAX(last_event_at) FROM ci_l1_pod_lifecycle WHERE last_event_at IS NOT NULL",
        db="ci",
    ),
    Check(
        name="archive_error_logs",
        level="MEDIUM",
        description="Pending error-log archivals within the last 4 hours",
        threshold_description="0 pending",
        sql=(
            "SELECT COUNT(*) FROM ci_l1_builds"
            " WHERE state IN ('failure','error','timeout','aborted')"
            " AND log_gcs_uri IS NULL"
            " AND completion_time > NOW() - INTERVAL 4 HOUR"
        ),
        db="ci",
        is_count_check=True,
    ),
    Check(
        name="prow_jobs",
        level="HIGH",
        description="Latest Prow job startTime in prow_jobs",
        threshold_description="4 hours",
        sql="SELECT MAX(startTime) FROM prow_jobs WHERE startTime IS NOT NULL",
        db="ci",
    ),
    Check(
        name="github_tickets",
        level="MEDIUM",
        description="Latest updated_at in github_tickets",
        threshold_description="30 hours",
        sql="SELECT MAX(updated_at) FROM github_tickets",
        db="ci",
    ),
    Check(
        name="ci_l1_flaky_issues",
        level="MEDIUM",
        description="ci-sync-flaky-issues job last succeeded at",
        threshold_description="30 hours",
        sql=(
            "SELECT last_succeeded_at FROM ci_job_state"
            " WHERE job_name = 'ci-sync-flaky-issues'"
        ),
        db="ci",
    ),
    Check(
        name="ci_l1_pr_events",
        level="MEDIUM",
        description="Latest PR event_time in ci_l1_pr_events",
        threshold_description="4 hours",
        sql="SELECT MAX(event_time) FROM ci_l1_pr_events WHERE event_time IS NOT NULL",
        db="ci",
    ),
    Check(
        name="problem_case_runs",
        level="MEDIUM",
        description="Latest report_time in problem_case_runs",
        threshold_description="4 hours",
        sql="SELECT MAX(report_time) FROM problem_case_runs WHERE report_time IS NOT NULL",
        db="ci",
    ),
    Check(
        name="ci_l1_builds_derived",
        level="MEDIUM",
        description="ci-refresh-build-derived job last succeeded at",
        threshold_description="4 hours",
        sql=(
            "SELECT last_succeeded_at FROM ci_job_state"
            " WHERE job_name = 'ci-refresh-build-derived'"
        ),
        db="ci",
    ),
    # --- Cost-Insight tables ---
    Check(
        name="cost_bq_export_summary_daily",
        level="MEDIUM",
        description="Latest GCP usage_date in cost_bq_export_summary_daily",
        threshold_description="4 days",
        sql=(
            "SELECT MAX(usage_date) FROM cost_bq_export_summary_daily"
            " WHERE vendor = 'GCP'"
        ),
        db="cost",
    ),
    Check(
        name="cost_attribution_daily",
        level="MEDIUM",
        description="Latest usage_date in cost_attribution_daily",
        threshold_description="4 days",
        sql="SELECT MAX(usage_date) FROM cost_attribution_daily",
        db="cost",
    ),
    Check(
        name="cost_unmatched_resource_daily",
        level="LOW",
        description="Latest usage_date in cost_unmatched_resource_daily",
        threshold_description="10 days",
        sql="SELECT MAX(usage_date) FROM cost_unmatched_resource_daily",
        db="cost",
    ),
    Check(
        name="sync_gcs_cache_last_seen",
        level="LOW",
        description="sync-gcs-cache-last-seen job last succeeded at",
        threshold_description="30 hours",
        sql=(
            "SELECT last_succeeded_at FROM cost_job_state"
            " WHERE job_name = 'sync-gcs-cache-last-seen'"
        ),
        db="cost",
    ),
    # --- Roster tables ---
    Check(
        name="roster_employees",
        level="MEDIUM",
        description="Latest updated_at in roster_employees",
        threshold_description="30 hours",
        sql="SELECT MAX(updated_at) FROM roster_employees",
        db="ci",
    ),
]


# ---------------------------------------------------------------------------
# Threshold helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(raw: str) -> datetime:
    """Parse an ISO-ish timestamp string into a datetime.

    Handles the TEXT representations that SQLite returns for DATETIME /
    DATE columns as well as standard MySQL formats.
    """
    # Try common ISO variants
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt
        except ValueError:
            continue
    # Last resort: try fromisoformat
    return datetime.fromisoformat(raw)


def _threshold_timedelta(threshold_description: str) -> timedelta:
    """Parse a threshold string like '4 hours', '30 hours', '4 days', '10 days'."""
    import re

    pattern = r"(\d+)\s*(hour|hours|day|days)"
    m = re.match(pattern, threshold_description.lower())
    if not m:
        raise ValueError(f"Unsupported threshold format: {threshold_description!r}")
    value = int(m.group(1))
    unit = m.group(2)
    if unit in ("hour", "hours"):
        return timedelta(hours=value)
    return timedelta(days=value)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    check: Check
    passed: bool
    value: Any  # raw DB result
    lag_description: str  # human-readable lag, e.g. "6h 30m" or "—"
    error: str | None = None
    skipped: bool = False  # True when the check was not executed (e.g. no DB)


@dataclass
class Report:
    timestamp: datetime
    results: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed and not r.skipped]

    @property
    def passed_all(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Engine helpers
# ---------------------------------------------------------------------------


def _build_engine_for_db(db: str, settings: Settings) -> Engine | None:
    """Build a SQLAlchemy engine for the named database ('ci' or 'cost').

    Returns None when *db=='cost'* and no cost-specific connection is
    configured, so callers can skip cost checks gracefully instead of
    accidentally querying the CI database.
    """
    if db == "cost":
        url = os.getenv("COST_INSIGHT_DB_URL")
        if url:
            from sqlalchemy import create_engine as _ce

            engine = _ce(url, pool_pre_ping=True, future=True)
            install_sqlite_functions(engine)
            return engine

        # Require explicit cost-insight env vars; do NOT fall back to CI DB.
        cost_user = os.getenv("COST_INSIGHT_TIDB_USER")
        if not cost_user:
            return None

        from sqlalchemy.engine import URL as _URL
        from sqlalchemy import create_engine as _ce

        url_obj = _URL.create(
            drivername="mysql+pymysql",
            username=cost_user,
            password=os.environ["COST_INSIGHT_TIDB_PASSWORD"],
            host=os.environ.get("COST_INSIGHT_TIDB_HOST", os.environ.get("TIDB_HOST", "")),
            port=int(os.environ.get("COST_INSIGHT_TIDB_PORT", os.environ.get("TIDB_PORT", "4000"))),
            database=os.environ.get("COST_INSIGHT_TIDB_DB", os.environ.get("TIDB_DB", "")),
            query={"charset": "utf8mb4"},
        )
        engine = _ce(url_obj, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_timeout=30, future=True)
        install_sqlite_functions(engine)
        return engine
    return build_engine(settings)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def run_check(connection: Connection, check: Check) -> CheckResult:
    """Execute a single freshness check and return the result."""
    try:
        row = connection.execute(text(check.sql)).fetchone()
    except Exception as exc:
        logger.error("Check %s failed with error: %s", check.name, exc)
        return CheckResult(
            check=check,
            passed=False,
            value=None,
            lag_description="query error",
            error=str(exc),
        )

    if row is None:
        return CheckResult(
            check=check,
            passed=False,
            value=None,
            lag_description="no data",
            error="query returned no rows",
        )

    raw_value = row[0]

    if raw_value is None:
        return CheckResult(
            check=check,
            passed=False,
            value=None,
            lag_description="NULL — no data yet",
            error="no timestamp found",
        )

    if check.is_count_check:
        count = int(raw_value)
        passed = count == 0
        return CheckResult(
            check=check,
            passed=passed,
            value=count,
            lag_description=f"{count} pending",
            error=None if passed else f"{count} rows pending archival",
        )

    # Timestamp check — coerce string / date → datetime so subtraction works.
    if isinstance(raw_value, str):
        raw_value = _parse_timestamp(raw_value)
    elif isinstance(raw_value, date) and not isinstance(raw_value, datetime):
        raw_value = datetime.combine(raw_value, datetime.min.time())
    now = datetime.now()

    lag: timedelta = now - raw_value  # type: ignore[operator]
    threshold = _threshold_timedelta(check.threshold_description)

    total_seconds = int(lag.total_seconds())
    if total_seconds < 0:
        total_seconds = 0

    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours >= 24:
        days = hours // 24
        hours = hours % 24
        lag_desc = f"{days}d {hours}h {minutes}m"
    else:
        lag_desc = f"{hours}h {minutes}m"

    passed = lag <= threshold

    if check.format_failure:
        lag_desc = check.format_failure(raw_value)

    return CheckResult(
        check=check,
        passed=passed,
        value=raw_value,
        lag_description=lag_desc,
    )


def run_all_checks(ci_engine: Engine, cost_engine: Engine | None) -> Report:
    """Run all freshness checks and return a Report."""
    results: list[CheckResult] = []

    with ci_engine.begin() as ci_conn:
        if cost_engine:
            with cost_engine.begin() as cost_conn:
                for check in CHECKS:
                    conn = cost_conn if check.db == "cost" else ci_conn
                    result = run_check(conn, check)
                    results.append(result)
        else:
            for check in CHECKS:
                if check.db == "cost":
                    results.append(
                        CheckResult(
                            check=check,
                            passed=True,
                            value=None,
                            lag_description="skipped — no cost db configured",
                            skipped=True,
                        )
                    )
                    continue
                result = run_check(ci_conn, check)
                results.append(result)

    return Report(
        timestamp=datetime.now(),
        results=results,
    )


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------


def _format_lark_message(report: Report) -> str:
    """Format the freshness report as a Lark incoming-webhook card message."""
    date_str = report.timestamp.strftime("%Y-%m-%d %H:%M UTC")

    failed_by_level: dict[str, list[CheckResult]] = {}
    for r in report.failed:
        failed_by_level.setdefault(r.check.level, []).append(r)

    passed = [r for r in report.results if r.passed and not r.skipped]
    skipped = [r for r in report.results if r.skipped]

    blocks: list[str] = []

    if not report.failed:
        parts = [f"✅ All {len(passed)} checks passed"]
        if skipped:
            parts.append(f"{len(skipped)} skipped")
        parts.append("— all data pipelines are fresh.")
        blocks.append(" ".join(parts))
    else:
        blocks.append(f"📊 Daily Data Freshness Check — {date_str}\n")

        for level in ("HIGH", "MEDIUM", "LOW"):
            items = failed_by_level.get(level, [])
            if not items:
                continue
            emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "⚪"}.get(level, "❓")
            blocks.append(f"{emoji} {level} ({len(items)}):")
            for r in items:
                blocks.append(
                    f"  • {r.check.description}: {r.lag_description}"
                    f" (threshold: {r.check.threshold_description})"
                )

        passed_count = len(passed)
        if passed_count:
            passed_names = [r.check.name for r in passed]
            blocks.append(f"\n✅ Passed ({passed_count}): {', '.join(passed_names)}")
        if skipped:
            skipped_names = [r.check.name for r in skipped]
            blocks.append(f"⏭️ Skipped ({len(skipped)}): {', '.join(skipped_names)}")

    return "\n".join(blocks)


def _send_lark_alert(webhook_url: str, text: str) -> None:
    """Send a text message to a Lark group via incoming webhook."""
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            logger.info("Lark webhook response: %s", body)
    except Exception as exc:
        logger.error("Failed to send Lark alert: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_check_data_freshness(settings: Settings) -> Report:
    """Entry point called from the CLI.

    Builds engines, runs all checks, and sends a Lark alert if configured.
    """
    ci_engine = build_engine(settings)
    cost_engine = _build_engine_for_db("cost", settings)
    if cost_engine is None:
        logger.warning(
            "Cost-Insight database not configured — cost checks will be skipped."
        )

    try:
        report = run_all_checks(ci_engine, cost_engine)
    finally:
        ci_engine.dispose()
        if cost_engine:
            cost_engine.dispose()

    webhook_url = os.getenv("LARK_ALERT_WEBHOOK_URL")
    dry_run = os.getenv("FRESHNESS_DRY_RUN", "false").lower() == "true"

    message = _format_lark_message(report)

    if dry_run:
        logger.info("DRY RUN — would send:\n%s", message)
        print(message)
    elif webhook_url:
        _send_lark_alert(webhook_url, message)
        logger.info("Alert sent: %d/%d checks failed", len(report.failed), len(report.results))
    else:
        logger.warning("LARK_ALERT_WEBHOOK_URL not set — alert not sent")
        print(message)

    # Log summary
    for r in report.failed:
        logger.warning(
            "CHECK FAILED [%s] %s: %s (threshold: %s)",
            r.check.level,
            r.check.name,
            r.lag_description,
            r.check.threshold_description,
        )

    return report
