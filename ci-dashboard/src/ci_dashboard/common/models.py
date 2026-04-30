from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class NormalizedBuildRow:
    source_prow_row_id: int | None
    source_prow_job_id: str | None
    namespace: str | None
    job_name: str | None
    job_type: str | None
    state: str
    optional: bool
    report: bool
    org: str | None
    repo: str | None
    repo_full_name: str | None
    base_ref: str | None
    pr_number: int | None
    is_pr_build: bool
    context: str | None
    url: str
    normalized_build_url: str | None
    author: str | None
    retest: bool | None
    event_guid: str | None
    build_id: str | None
    pod_name: str | None
    pending_time: datetime | None
    start_time: datetime | None
    completion_time: datetime | None
    queue_wait_seconds: int | None
    run_seconds: int | None
    total_seconds: int | None
    head_sha: str | None
    target_branch: str | None
    cloud_phase: str
    build_system: str
    is_flaky: bool
    is_retry_loop: bool
    has_flaky_case_match: bool
    failure_category: str | None
    failure_subcategory: str | None

    def as_db_params(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("optional", "report", "is_pr_build", "is_flaky", "is_retry_loop", "has_flaky_case_match"):
            data[key] = int(bool(data[key]))
        if data["retest"] is not None:
            data["retest"] = int(bool(data["retest"]))
        return data


@dataclass(frozen=True)
class JobState:
    job_name: str
    watermark: dict[str, Any]
    last_started_at: datetime | None
    last_succeeded_at: datetime | None
    last_status: str
    last_error: str | None


@dataclass
class SyncBuildsSummary:
    batches_processed: int = 0
    source_rows_scanned: int = 0
    rows_written: int = 0
    rows_skipped: int = 0
    last_source_prow_row_id: int = 0


@dataclass
class SyncPrEventsSummary:
    batches_processed: int = 0
    candidate_prs: int = 0
    ticket_rows_fetched: int = 0
    events_written: int = 0
    last_build_source_prow_row_id_seen: int = 0
    last_ticket_updated_at: str | None = None


@dataclass
class RefreshBuildDerivedSummary:
    impacted_builds: int = 0
    groups_recomputed: int = 0
    branch_rows_updated: int = 0
    flaky_rows_updated: int = 0
    case_match_rows_updated: int = 0
    failure_category_rows_updated: int = 0
    last_processed_build_id: int = 0
    last_processed_pr_event_updated_at: str | None = None
    last_processed_case_report_time: str | None = None


@dataclass
class SyncFlakyIssuesSummary:
    source_rows_scanned: int = 0
    rows_written: int = 0
    issue_pr_links_written: int = 0
    branch_fetch_attempted: int = 0
    branch_fetch_failed: int = 0
    last_ticket_updated_at: str | None = None


@dataclass
class BackfillFlakyIssuePrLinksSummary:
    batches_processed: int = 0
    source_rows_scanned: int = 0
    issue_rows_touched: int = 0
    issue_pr_links_written: int = 0
    last_ticket_updated_at: str | None = None


@dataclass
class SyncPodsSummary:
    batches_processed: int = 0
    source_rows_scanned: int = 0
    event_rows_written: int = 0
    lifecycle_rows_upserted: int = 0
    reconciled_rows_updated: int = 0
    pods_touched: int = 0
    last_receive_timestamp: str | None = None


@dataclass
class ConsumeJenkinsEventsSummary:
    messages_polled: int = 0
    events_processed: int = 0
    events_skipped: int = 0
    events_failed: int = 0
    build_rows_written: int = 0


@dataclass
class ArchiveErrorLogsSummary:
    builds_scanned: int = 0
    builds_archived: int = 0
    builds_skipped: int = 0
    builds_failed: int = 0


@dataclass(frozen=True)
class ErrorClassification:
    l1_category: str
    l2_subcategory: str
    source: str


@dataclass
class AnalyzeErrorsSummary:
    builds_scanned: int = 0
    builds_classified: int = 0
    builds_rule_classified: int = 0
    builds_llm_classified: int = 0
    builds_skipped: int = 0
    builds_failed: int = 0


@dataclass
class ReviewErrorSummary:
    rows_updated: int = 0


@dataclass
class RepairJobNamesSummary:
    build_rows_updated: int = 0
    pod_rows_updated: int = 0
    build_short_before: int = 0
    build_short_after: int = 0
    pod_short_before: int = 0
    pod_short_after: int = 0
