from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from html.parser import HTMLParser
import logging
import re
from threading import BoundedSemaphore
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import BackfillJenkinsTimingsSummary
from ci_dashboard.jobs.jenkins_client import JenkinsClient

LOG = logging.getLogger(__name__)

TIMINGS_FETCH_TIMEOUT_SECONDS = 5
TIMINGS_WORKERS = 2
TIMINGS_MAX_PENDING = 32

UPDATE_JENKINS_TIMINGS = text(
    """
    UPDATE ci_l1_builds
    SET jenkins_blocked_subtasks_sum = :jenkins_blocked_subtasks_sum,
        jenkins_buildable_subtasks_sum = :jenkins_buildable_subtasks_sum,
        jenkins_queue_total_subtasks_sum = :jenkins_queue_total_subtasks_sum,
        jenkins_building_subtasks_sum = :jenkins_building_subtasks_sum,
        jenkins_subtask_count = :jenkins_subtask_count,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :id
    """
)


@dataclass(frozen=True)
class JenkinsTimings:
    blocked_subtasks_sum: int
    buildable_subtasks_sum: int
    queue_total_subtasks_sum: int
    building_subtasks_sum: int
    subtask_count: int

    def as_db_params(self, *, build_id: int) -> dict[str, int]:
        return {
            "id": build_id,
            "jenkins_blocked_subtasks_sum": self.blocked_subtasks_sum,
            "jenkins_buildable_subtasks_sum": self.buildable_subtasks_sum,
            "jenkins_queue_total_subtasks_sum": self.queue_total_subtasks_sum,
            "jenkins_building_subtasks_sum": self.building_subtasks_sum,
            "jenkins_subtask_count": self.subtask_count,
        }


class _TimingsTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._in_table = False
        self._table_depth = 0
        self._in_row = False
        self._in_cell = False
        self._row: list[str] = []
        self._cell_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            classes = dict(attrs).get("class") or ""
            if self._in_table:
                self._table_depth += 1
            elif "jenkins-table" in classes.split():
                self._in_table = True
                self._table_depth = 1
            return
        if not self._in_table:
            return
        if tag == "tr":
            self._in_row = True
            self._row = []
        elif tag in {"td", "th"} and self._in_row:
            self._in_cell = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._in_table:
            self._table_depth -= 1
            if self._table_depth == 0:
                self._in_table = False
            return
        if not self._in_table:
            return
        if tag in {"td", "th"} and self._in_cell:
            self._row.append(" ".join("".join(self._cell_parts).split()))
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if self._row:
                self.rows.append(self._row)
            self._in_row = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)


def parse_jenkins_timings(html_text: str) -> JenkinsTimings:
    parser = _TimingsTableParser()
    parser.feed(html_text)

    values: dict[str, str] = {}
    for row in parser.rows:
        normalized = [cell.strip() for cell in row if cell.strip()]
        if len(normalized) < 2:
            continue
        label = normalized[0].lower()
        if label in {
            "blocked",
            "buildable",
            "total",
            "building",
            "number of subtasks",
        }:
            values[label] = normalized[-1]

    required = {"blocked", "buildable", "total", "building", "number of subtasks"}
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError(f"Jenkins timings table is missing rows: {', '.join(missing)}")

    return JenkinsTimings(
        blocked_subtasks_sum=parse_jenkins_duration_seconds(values["blocked"]),
        buildable_subtasks_sum=parse_jenkins_duration_seconds(values["buildable"]),
        queue_total_subtasks_sum=parse_jenkins_duration_seconds(values["total"]),
        building_subtasks_sum=parse_jenkins_duration_seconds(values["building"]),
        subtask_count=int(values["number of subtasks"]),
    )


def parse_jenkins_duration_seconds(value: str) -> int:
    normalized = value.strip().lower()
    matches = re.findall(
        r"(\d+(?:\.\d+)?)\s*(days?|hrs?|hours?|mins?|minutes?|secs?|seconds?|ms)",
        normalized,
    )
    if not matches:
        raise ValueError(f"unsupported Jenkins duration: {value!r}")

    multipliers = {
        "day": Decimal(86400),
        "days": Decimal(86400),
        "hr": Decimal(3600),
        "hrs": Decimal(3600),
        "hour": Decimal(3600),
        "hours": Decimal(3600),
        "min": Decimal(60),
        "mins": Decimal(60),
        "minute": Decimal(60),
        "minutes": Decimal(60),
        "sec": Decimal(1),
        "secs": Decimal(1),
        "second": Decimal(1),
        "seconds": Decimal(1),
        "ms": Decimal("0.001"),
    }
    seconds = sum(
        (Decimal(amount) * multipliers[unit] for amount, unit in matches),
        start=Decimal(0),
    )
    return int(seconds.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fetch_and_store_jenkins_timings(
    engine: Engine,
    *,
    build_id: int,
    build_url: str,
    fetcher: Any,
) -> None:
    html_text = fetcher.fetch_timings_html(
        build_url,
        timeout_seconds=TIMINGS_FETCH_TIMEOUT_SECONDS,
    )
    timings = parse_jenkins_timings(html_text)
    with engine.begin() as connection:
        connection.execute(
            UPDATE_JENKINS_TIMINGS,
            timings.as_db_params(build_id=build_id),
        )


class JenkinsTimingsEnricher:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        fetcher: Any | None = None,
        max_workers: int = TIMINGS_WORKERS,
        max_pending: int = TIMINGS_MAX_PENDING,
    ) -> None:
        self._engine = engine
        self._fetcher = fetcher or JenkinsClient(settings.jenkins)
        self._owns_fetcher = fetcher is None
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="jenkins-timings",
        )
        self._slots = BoundedSemaphore(max_pending)

    def submit(self, *, build_id: int, build_url: str) -> bool:
        if not self._slots.acquire(blocking=False):
            LOG.warning(
                "dropping Jenkins timings fetch because the async queue is full",
                extra={"build_id": build_id, "build_url": build_url},
            )
            return False
        try:
            future = self._executor.submit(
                self._run,
                build_id=build_id,
                build_url=build_url,
            )
        except Exception:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future: self._slots.release())
        return True

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        if self._owns_fetcher and hasattr(self._fetcher, "close"):
            self._fetcher.close()

    def _run(self, *, build_id: int, build_url: str) -> None:
        try:
            fetch_and_store_jenkins_timings(
                self._engine,
                build_id=build_id,
                build_url=build_url,
                fetcher=self._fetcher,
            )
        except Exception:
            LOG.exception(
                "failed to enrich Jenkins timings",
                extra={"build_id": build_id, "build_url": build_url},
            )


def run_backfill_jenkins_timings(
    engine: Engine,
    settings: Settings,
    *,
    lookback_days: int = 30,
    limit: int | None = None,
    fetcher: Any | None = None,
) -> BackfillJenkinsTimingsSummary:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=lookback_days)
    limit_clause = "LIMIT :limit_value" if limit is not None else ""
    query = text(
        f"""
        SELECT id, COALESCE(normalized_build_url, url) AS build_url
        FROM ci_l1_builds
        WHERE build_system = 'JENKINS'
          AND completion_time IS NOT NULL
          AND completion_time >= :cutoff
          AND jenkins_queue_total_subtasks_sum IS NULL
        ORDER BY completion_time, id
        {limit_clause}
        """
    )
    params: dict[str, Any] = {"cutoff": cutoff}
    if limit is not None:
        params["limit_value"] = limit
    with engine.begin() as connection:
        candidates = list(connection.execute(query, params).mappings())

    summary = BackfillJenkinsTimingsSummary()
    resolved_fetcher = fetcher or JenkinsClient(settings.jenkins)
    owns_fetcher = fetcher is None
    try:
        for build in candidates:
            summary.builds_scanned += 1
            try:
                fetch_and_store_jenkins_timings(
                    engine,
                    build_id=int(build["id"]),
                    build_url=str(build["build_url"]),
                    fetcher=resolved_fetcher,
                )
            except Exception:
                summary.builds_failed += 1
                LOG.exception(
                    "failed to backfill Jenkins timings",
                    extra={"build_id": build["id"], "build_url": build["build_url"]},
                )
            else:
                summary.builds_updated += 1
            if summary.builds_scanned % 100 == 0:
                LOG.info(
                    "Jenkins timings backfill progress",
                    extra={
                        "builds_scanned": summary.builds_scanned,
                        "builds_total": len(candidates),
                        "builds_updated": summary.builds_updated,
                        "builds_failed": summary.builds_failed,
                    },
                )
    finally:
        if owns_fetcher and hasattr(resolved_fetcher, "close"):
            resolved_fetcher.close()
    return summary
