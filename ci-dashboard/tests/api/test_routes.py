from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.main import app, create_app
from ci_dashboard.api.queries import cost as cost_queries
from ci_dashboard.api.queries import pages as page_queries
from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.jobs.build_url_matcher import normalize_build_url


def test_get_engine_dependency_caches_underlying_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from ci_dashboard.api import dependencies as dependencies_module

    sentinel = object()
    calls = {"count": 0}

    dependencies_module._cached_engine.cache_clear()

    def fake_build_engine():
        calls["count"] += 1
        return sentinel

    monkeypatch.setattr("ci_dashboard.api.dependencies.build_engine", fake_build_engine)

    assert get_engine() is sentinel
    assert get_engine() is sentinel
    assert calls["count"] == 1

    dependencies_module._cached_engine.cache_clear()


def test_cost_null_safe_eq_uses_dialect_specific_operator() -> None:
    sqlite_connection = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    mysql_connection = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    assert cost_queries._null_safe_eq(sqlite_connection, "m.org", "r.org") == "m.org IS r.org"
    assert cost_queries._null_safe_eq(mysql_connection, "m.org", "r.org") == "m.org <=> r.org"


def _insert_build(
    sqlite_engine,
    *,
    source_prow_row_id: int,
    source_prow_job_id: str,
    repo_full_name: str,
    target_branch: str | None,
    base_ref: str | None,
    job_name: str,
    state: str,
    cloud_phase: str,
    is_flaky: int,
    is_retry_loop: int,
    failure_category: str | None,
    start_time: str,
    queue_wait_seconds: int = 0,
    run_seconds: int = 0,
    total_seconds: int = 0,
    pr_number: int = 100,
    normalized_build_url: str | None = None,
    build_id: str = "1",
    author: str = "alice",
    error_l1_category: str | None = None,
    error_l2_subcategory: str | None = None,
) -> None:
    org, repo = repo_full_name.split("/", 1)
    build_url = normalize_build_url(normalized_build_url or f"/jenkins/job/{source_prow_job_id}")
    assert build_url is not None
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, base_ref, pr_number, is_pr_build,
                  context, url, normalized_build_url, author, retest, event_guid, build_id,
                  pod_name, pending_time, start_time, completion_time, queue_wait_seconds,
                  run_seconds, total_seconds, head_sha, target_branch, cloud_phase, is_flaky,
                  is_retry_loop, has_flaky_case_match, failure_category, failure_subcategory,
                  error_l1_category, error_l2_subcategory
                ) VALUES (
                  :source_prow_row_id, :source_prow_job_id, 'prow', :job_name, 'presubmit', :state,
                  0, 1, :org, :repo, :repo_full_name, :base_ref, :pr_number, 1,
                  'unit-test', :url,
                  :normalized_build_url, :author, 0, 'guid', :build_id, NULL, NULL, :start_time,
                  :start_time, :queue_wait_seconds, :run_seconds, :total_seconds, 'sha', :target_branch, :cloud_phase, :is_flaky,
                  :is_retry_loop, 0, :failure_category, NULL, :error_l1_category, :error_l2_subcategory
                )
                """
            ),
            {
                "source_prow_row_id": source_prow_row_id,
                "source_prow_job_id": source_prow_job_id,
                "job_name": job_name,
                "state": state,
                "org": org,
                "repo": repo,
                "repo_full_name": repo_full_name,
                "base_ref": base_ref,
                "pr_number": pr_number,
                "url": f"{build_url}display/redirect",
                "normalized_build_url": build_url,
                "build_id": build_id,
                "author": author,
                "start_time": start_time,
                "queue_wait_seconds": queue_wait_seconds,
                "run_seconds": run_seconds,
                "total_seconds": total_seconds,
                "target_branch": target_branch,
                "cloud_phase": cloud_phase,
                "is_flaky": is_flaky,
                "is_retry_loop": is_retry_loop,
                "failure_category": failure_category,
                "error_l1_category": error_l1_category,
                "error_l2_subcategory": error_l2_subcategory,
            },
        )


def _insert_success_run_series(
    sqlite_engine,
    *,
    start_source_prow_row_id: int,
    source_prefix: str,
    repo_full_name: str,
    target_branch: str,
    job_name: str,
    cloud_phase: str,
    normalized_job_path: str,
    start_times: list[str],
    run_seconds_list: list[int],
) -> None:
    for index, (start_time, run_seconds) in enumerate(zip(start_times, run_seconds_list), start=0):
        _insert_build(
            sqlite_engine,
            source_prow_row_id=start_source_prow_row_id + index,
            source_prow_job_id=f"{source_prefix}-{index + 1}",
            repo_full_name=repo_full_name,
            target_branch=target_branch,
            base_ref=target_branch,
            job_name=job_name,
            state="success",
            cloud_phase=cloud_phase,
            is_flaky=0,
            is_retry_loop=0,
            failure_category=None,
            start_time=start_time,
            queue_wait_seconds=max(run_seconds // 10, 1),
            run_seconds=run_seconds,
            total_seconds=run_seconds + max(run_seconds // 10, 1),
            pr_number=300 + start_source_prow_row_id + index,
            normalized_build_url=f"{normalized_job_path.rstrip('/')}/{start_source_prow_row_id + index}/",
            build_id=f"prow-{start_source_prow_row_id + index}",
        )


def _insert_pr_event(
    sqlite_engine,
    *,
    repo: str,
    pr_number: int,
    target_branch: str,
    event_key: str,
    event_time: str,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pr_events (
                  repo, pr_number, event_key, event_time, event_type, actor_login, comment_id,
                  comment_body, retest_event, commit_sha, target_branch, head_ref, head_sha,
                  created_at, updated_at
                ) VALUES (
                  :repo, :pr_number, :event_key, :event_time, 'pr_snapshot', NULL, NULL,
                  NULL, 0, NULL, :target_branch, 'feature-x', 'sha', :event_time, :event_time
                )
                """
            ),
            {
                "repo": repo,
                "pr_number": pr_number,
                "event_key": event_key,
                "event_time": event_time,
                "target_branch": target_branch,
            },
        )


def _insert_problem_case_run(
    sqlite_engine,
    *,
    repo: str,
    branch: str,
    case_name: str,
    build_url: str,
    flaky: int,
    report_time: str,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO problem_case_runs (
                  repo, branch, suite_name, case_name, flaky, timecost_ms, report_time, build_url, reason
                ) VALUES (
                  :repo, :branch, 'unit', :case_name, :flaky, 10, :report_time, :build_url, 'flake'
                )
                """
            ),
            {
                "repo": repo,
                "branch": branch,
                "case_name": case_name,
                "build_url": build_url,
                "flaky": flaky,
                "report_time": report_time,
            },
        )


def _insert_flaky_issue(
    sqlite_engine,
    *,
    repo: str,
    issue_number: int,
    case_name: str,
    issue_branch: str,
    issue_status: str,
    issue_created_at: str,
    issue_closed_at: str | None = None,
    last_reopened_at: str | None = None,
    reopen_count: int = 0,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_flaky_issues (
                  repo, issue_number, issue_url, issue_title, case_name, issue_status, issue_branch,
                  branch_source, issue_created_at, issue_updated_at, issue_closed_at, last_reopened_at,
                  reopen_count, source_ticket_id, source_ticket_updated_at
                ) VALUES (
                  :repo, :issue_number, :issue_url, :issue_title, :case_name, :issue_status, :issue_branch,
                  'issue_html', :issue_created_at, :issue_updated_at, :issue_closed_at, :last_reopened_at,
                  :reopen_count, :source_ticket_id, :source_ticket_updated_at
                )
                """
            ),
            {
                "repo": repo,
                "issue_number": issue_number,
                "issue_url": f"https://github.com/{repo}/issues/{issue_number}",
                "issue_title": f"Flaky test: {case_name} in nightly",
                "case_name": case_name,
                "issue_status": issue_status,
                "issue_branch": issue_branch,
                "issue_created_at": issue_created_at,
                "issue_updated_at": issue_created_at,
                "issue_closed_at": issue_closed_at,
                "last_reopened_at": last_reopened_at,
                "reopen_count": reopen_count,
                "source_ticket_id": issue_number,
                "source_ticket_updated_at": issue_created_at,
            },
        )


def _insert_flaky_issue_pr_link(
    sqlite_engine,
    *,
    issue_repo: str,
    issue_number: int,
    pr_repo: str,
    pr_number: int,
    linked_at: str,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_flaky_issue_pr_links (
                  issue_repo, issue_number, pr_repo, pr_number, pr_url, pr_title,
                  link_type, source_event_type, source_event_id, linked_at, source_ticket_updated_at
                ) VALUES (
                  :issue_repo, :issue_number, :pr_repo, :pr_number, :pr_url, :pr_title,
                  'linked_pull_request', 'cross-referenced', NULL, :linked_at, :linked_at
                )
                """
            ),
            {
                "issue_repo": issue_repo,
                "issue_number": issue_number,
                "pr_repo": pr_repo,
                "pr_number": pr_number,
                "pr_url": f"https://github.com/{pr_repo}/pull/{pr_number}",
                "pr_title": f"Fix flaky issue #{issue_number}",
                "linked_at": linked_at,
            },
        )


def _insert_pull_ticket(
    sqlite_engine,
    *,
    repo: str,
    number: int,
    state: str,
    created_at: str,
    closed_at: str | None = None,
    merged: int = 0,
    merged_at: str | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_flaky_linked_prs (
                  pr_repo, pr_number, pr_url, pr_title, pr_state, pr_created_at,
                  pr_closed_at, pr_merged_at
                ) VALUES (
                  :repo, :number, :pr_url, :title, :state, :created_at,
                  :closed_at, :merged_at
                )
                """
            ),
            {
                "repo": repo,
                "number": number,
                "pr_url": f"https://github.com/{repo}/pull/{number}",
                "title": f"PR {number}",
                "state": state,
                "created_at": created_at,
                "closed_at": closed_at,
                "merged_at": merged_at if merged else None,
            },
        )


def _insert_job_state(
    sqlite_engine,
    *,
    job_name: str,
    last_status: str,
    last_succeeded_at: str | None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_job_state (
                  job_name, watermark_json, last_started_at, last_succeeded_at, last_status, last_error, updated_at
                ) VALUES (
                  :job_name, '{}', :last_succeeded_at, :last_succeeded_at, :last_status, NULL, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "job_name": job_name,
                "last_status": last_status,
                "last_succeeded_at": last_succeeded_at,
            },
        )


def _insert_roster_group(
    sqlite_engine,
    *,
    group_id: int,
    lark_group_id: str,
    name: str,
    path: str,
    parent_id: int | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO roster_groups (
                  id, lark_group_id, parent_id, name, path, is_active
                ) VALUES (
                  :id, :lark_group_id, :parent_id, :name, :path, 1
                )
                """
            ),
            {
                "id": group_id,
                "lark_group_id": lark_group_id,
                "parent_id": parent_id,
                "name": name,
                "path": path,
            },
        )


def _insert_cost_source(
    sqlite_engine,
    *,
    vendor: str,
    account_id: str,
    display_name: str | None = None,
    billing_account_id: str | None = None,
    is_active: int = 1,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_sources (
                  vendor, account_id, billing_account_id, display_name, is_active
                ) VALUES (
                  :vendor, :account_id, :billing_account_id, :display_name, :is_active
                )
                """
            ),
            {
                "vendor": vendor,
                "account_id": account_id,
                "billing_account_id": billing_account_id,
                "display_name": display_name,
                "is_active": is_active,
            },
        )


def _insert_cost_budget(
    sqlite_engine,
    *,
    vendor: str,
    account_id: str,
    period_start_date: str,
    period_end_date: str,
    budget_amount: float,
    budget_name: str | None = None,
    label_filters: dict | list | str | None = None,
    group_id: int | None = None,
    manager_id: int | None = None,
    repo: str | None = None,
    filter_hash: str | None = None,
    source_type: str = "manual",
    source_ref: str | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_budgets (
                  vendor, account_id, period_start_date, period_end_date, budget_name,
                  label_filters, filter_hash, group_id, manager_id, repo, budget_amount,
                  source_type, source_ref
                ) VALUES (
                  :vendor, :account_id, :period_start_date, :period_end_date, :budget_name,
                  :label_filters, :filter_hash, :group_id, :manager_id, :repo, :budget_amount,
                  :source_type, :source_ref
                )
                """
            ),
            {
                "vendor": vendor,
                "account_id": account_id,
                "period_start_date": period_start_date,
                "period_end_date": period_end_date,
                "budget_name": budget_name,
                "label_filters": (
                    json.dumps(label_filters, sort_keys=True)
                    if isinstance(label_filters, (dict, list))
                    else label_filters
                ),
                "filter_hash": filter_hash
                or f"{vendor}:{account_id}:{period_start_date}:{period_end_date}:{budget_name or 'scope'}",
                "group_id": group_id,
                "manager_id": manager_id,
                "repo": repo,
                "budget_amount": budget_amount,
                "source_type": source_type,
                "source_ref": source_ref,
            },
        )


def _insert_cost_attribution(
    sqlite_engine,
    *,
    usage_date: str,
    repo: str,
    group_id: int,
    net_cost: float,
    vendor: str = "gcp",
    account_id: str = "pingcap-testing-account",
    effective_cost: float | None = None,
    list_cost: float | None = None,
    dimension_hash: str | None = None,
    resource_name: str | None = None,
    author: str | None = "alice",
    owner: str | None = "alice",
    attribution_key: str | None = "employee:1",
    attribution_source: str = "author_github",
    attribution_status: str = "matched",
    employee_id: int | None = 1,
    usage_seconds: float = 3600,
    service_name: str = "Compute Engine",
    target_branch: str | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_attribution_daily (
                  usage_date, vendor, account_id, service_name, sku_name, org, repo,
                  target_branch, resource_name, author, owner, attribution_key, attribution_source,
                  attribution_status, employee_id, group_id, manager_id, usage_seconds,
                  list_cost, effective_cost, credit_amount, net_cost, source_rows, dimension_hash
                ) VALUES (
                  :usage_date, :vendor, :account_id, :service_name, 'runner',
                  'pingcap', :repo, :target_branch, :resource_name, :author, :owner, :attribution_key, :attribution_source,
                  :attribution_status, :employee_id, :group_id, NULL, :usage_seconds, :list_cost, :effective_cost,
                  0, :net_cost, 1, :dimension_hash
                )
                """
            ),
            {
                "usage_date": usage_date,
                "vendor": vendor,
                "account_id": account_id,
                "repo": repo,
                "target_branch": target_branch,
                "group_id": group_id,
                "resource_name": resource_name,
                "author": author,
                "owner": owner,
                "attribution_key": attribution_key,
                "attribution_source": attribution_source,
                "attribution_status": attribution_status,
                "employee_id": employee_id,
                "usage_seconds": usage_seconds,
                "service_name": service_name,
                "list_cost": list_cost if list_cost is not None else net_cost,
                "effective_cost": effective_cost if effective_cost is not None else net_cost,
                "net_cost": net_cost,
                "dimension_hash": dimension_hash or f"{usage_date}-{repo}-{group_id}-{net_cost}",
            },
        )


def _insert_cost_raw_detail(
    sqlite_engine,
    *,
    usage_date: str,
    repo: str,
    resource_name: str,
    namespace: str | None,
    list_cost: float,
    effective_cost: float | None = None,
    net_cost: float | None = None,
    author: str | None = "alice",
    usage_seconds: float = 3600,
    service_name: str = "Compute Engine",
    sku_name: str = "runner",
    source_row_hash: str | None = None,
    target_branch: str | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_raw_details (
                  vendor, account_id, billing_account_id, usage_date, service_name, sku_name,
                  region, namespace, author, org, repo, target_branch, resource_name, usage_seconds,
                  list_cost, effective_cost, credit_amount, net_cost, source_export_time, source_row_hash
                ) VALUES (
                  'gcp', 'pingcap-testing-account', 'billing-account-1', :usage_date, :service_name, :sku_name,
                  'us-central1', :namespace, :author, 'pingcap', :repo, :target_branch, :resource_name, :usage_seconds,
                  :list_cost, :effective_cost, 0, :net_cost, '2026-05-19 00:00:00', :source_row_hash
                )
                """
            ),
            {
                "usage_date": usage_date,
                "service_name": service_name,
                "sku_name": sku_name,
                "namespace": namespace,
                "author": author,
                "repo": repo,
                "target_branch": target_branch,
                "resource_name": resource_name,
                "usage_seconds": usage_seconds,
                "list_cost": list_cost,
                "effective_cost": effective_cost if effective_cost is not None else list_cost,
                "net_cost": net_cost if net_cost is not None else list_cost,
                "source_row_hash": source_row_hash or f"{usage_date}-{resource_name}-{namespace}-{list_cost}",
            },
        )


def _insert_cost_unmatched_resource(
    sqlite_engine,
    *,
    usage_date: str,
    repo: str,
    resource_name: str,
    namespace: str | None,
    list_cost: float,
    vendor: str = "gcp",
    account_id: str = "pingcap-testing-account",
    billing_account_id: str = "billing-account-1",
    effective_cost: float | None = None,
    net_cost: float | None = None,
    author: str | None = "alice",
    usage_seconds: float = 3600,
    service_name: str = "Compute Engine",
    sku_name: str = "runner",
    source_row_hash: str | None = None,
    target_branch: str | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_unmatched_resource_daily (
                  vendor, account_id, billing_account_id, export_partition_date, usage_date,
                  service_name, sku_name, namespace, author, org, repo, target_branch, resource_name,
                  usage_seconds, list_cost, effective_cost, credit_amount, net_cost,
                  source_export_time, source_row_hash
                ) VALUES (
                  :vendor, :account_id, :billing_account_id, :usage_date, :usage_date,
                  :service_name, :sku_name, :namespace, :author, 'pingcap', :repo, :target_branch, :resource_name,
                  :usage_seconds, :list_cost, :effective_cost, 0, :net_cost,
                  '2026-05-19 00:00:00', :source_row_hash
                )
                """
            ),
            {
                "usage_date": usage_date,
                "vendor": vendor,
                "account_id": account_id,
                "billing_account_id": billing_account_id,
                "service_name": service_name,
                "sku_name": sku_name,
                "namespace": namespace,
                "author": author,
                "repo": repo,
                "target_branch": target_branch,
                "resource_name": resource_name,
                "usage_seconds": usage_seconds,
                "list_cost": list_cost,
                "effective_cost": effective_cost if effective_cost is not None else list_cost,
                "net_cost": net_cost if net_cost is not None else list_cost,
                "source_row_hash": source_row_hash
                or f"{usage_date}-{resource_name}-{namespace}-{list_cost}",
            },
        )


@pytest.fixture()
def api_client(sqlite_engine, monkeypatch):
    _insert_build(
        sqlite_engine,
        source_prow_row_id=1,
        source_prow_job_id="job-1",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-a",
        state="failure",
        cloud_phase="GCP",
        is_flaky=1,
        is_retry_loop=0,
        failure_category="FLAKY_TEST",
        start_time="2026-04-10 10:00:00",
        queue_wait_seconds=30,
        run_seconds=300,
        total_seconds=330,
        pr_number=100,
        error_l1_category="INFRA",
        error_l2_subcategory="DISK_FULL",
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=2,
        source_prow_job_id="job-2",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-a",
        state="failure",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=1,
        failure_category="FLAKY_TEST",
        start_time="2026-04-10 10:05:00",
        queue_wait_seconds=45,
        run_seconds=180,
        total_seconds=225,
        pr_number=100,
        error_l1_category="BUILD",
        error_l2_subcategory="COMPILE",
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=3,
        source_prow_job_id="job-3",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-b",
        state="success",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-10 10:10:00",
        queue_wait_seconds=120,
        run_seconds=600,
        total_seconds=720,
        pr_number=101,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=4,
        source_prow_job_id="job-4",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-b",
        state="failure",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-11 10:00:00",
        queue_wait_seconds=15,
        run_seconds=90,
        total_seconds=105,
        pr_number=102,
        error_l1_category="INFRA",
        error_l2_subcategory="JENKINS",
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=5,
        source_prow_job_id="job-5",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="job-c",
        state="timeout",
        cloud_phase="GCP",
        is_flaky=1,
        is_retry_loop=0,
        failure_category="FLAKY_TEST",
        start_time="2026-04-11 10:10:00",
        queue_wait_seconds=20,
        run_seconds=150,
        total_seconds=170,
        pr_number=102,
        error_l1_category="BUILD",
        error_l2_subcategory="COMPILE",
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=6,
        source_prow_job_id="job-6",
        repo_full_name="pingcap/tiflash",
        target_branch="release-8.5",
        base_ref="release-8.5",
        job_name="job-d",
        state="failure",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-11 11:00:00",
        queue_wait_seconds=25,
        run_seconds=200,
        total_seconds=225,
        pr_number=200,
        author="bob",
        error_l1_category="OTHERS",
        error_l2_subcategory="UNCLASSIFIED",
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=7,
        source_prow_job_id="job-7",
        repo_full_name="pingcap/tidb",
        target_branch="main",
        base_ref="main",
        job_name="job-e",
        state="success",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-11 11:30:00",
        queue_wait_seconds=90,
        run_seconds=450,
        total_seconds=540,
        pr_number=103,
        author="carol",
    )
    _insert_success_run_series(
        sqlite_engine,
        start_source_prow_row_id=100,
        source_prefix="job-fast-idc",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        job_name="job-fast",
        cloud_phase="IDC",
        normalized_job_path="/jenkins/job/pingcap/job/tidb/job/job-fast",
        start_times=[
            "2026-02-20 09:00:00",
            "2026-02-21 09:00:00",
            "2026-02-22 09:00:00",
            "2026-02-23 09:00:00",
            "2026-02-24 09:00:00",
        ],
        run_seconds_list=[600, 620, 580, 610, 590],
    )
    _insert_success_run_series(
        sqlite_engine,
        start_source_prow_row_id=110,
        source_prefix="job-fast-gcp",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        job_name="job-fast",
        cloud_phase="GCP",
        normalized_job_path="/jenkins/job/pingcap/job/tidb/job/job-fast",
        start_times=[
            "2026-03-01 09:00:00",
            "2026-04-02 09:00:00",
            "2026-04-03 09:00:00",
            "2026-04-04 09:00:00",
            "2026-04-05 09:00:00",
            "2026-04-06 09:00:00",
        ],
        run_seconds_list=[500, 300, 320, 280, 290, 310],
    )
    _insert_success_run_series(
        sqlite_engine,
        start_source_prow_row_id=120,
        source_prefix="job-slow-idc",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        job_name="job-slow",
        cloud_phase="IDC",
        normalized_job_path="/jenkins/job/pingcap/job/tidb/job/job-slow",
        start_times=[
            "2026-02-20 10:00:00",
            "2026-02-21 10:00:00",
            "2026-02-22 10:00:00",
            "2026-02-23 10:00:00",
            "2026-02-24 10:00:00",
        ],
        run_seconds_list=[180, 200, 220, 210, 190],
    )
    _insert_success_run_series(
        sqlite_engine,
        start_source_prow_row_id=130,
        source_prefix="job-slow-gcp",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        job_name="job-slow",
        cloud_phase="GCP",
        normalized_job_path="/jenkins/job/pingcap/job/tidb/job/job-slow",
        start_times=[
            "2026-03-01 10:00:00",
            "2026-04-02 10:00:00",
            "2026-04-03 10:00:00",
            "2026-04-04 10:00:00",
            "2026-04-05 10:00:00",
            "2026-04-06 10:00:00",
        ],
        run_seconds_list=[210, 480, 500, 520, 510, 490],
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=100,
        target_branch="master",
        event_key="pr-100-master",
        event_time="2026-04-10 09:30:00",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=101,
        target_branch="master",
        event_key="pr-101-master",
        event_time="2026-04-10 09:35:00",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=102,
        target_branch="master",
        event_key="pr-102-master",
        event_time="2026-04-11 09:35:00",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tiflash",
        pr_number=200,
        target_branch="release-8.5",
        event_key="pr-200-release85",
        event_time="2026-04-11 10:30:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestCaseAlpha",
        build_url="https://prow.tidb.net/jenkins/job/job-1/display/redirect",
        flaky=1,
        report_time="2026-04-10 10:20:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestCaseBeta",
        build_url="https://prow.tidb.net/jenkins/job/job-5/display/redirect",
        flaky=1,
        report_time="2026-04-11 10:20:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=66726,
        case_name="TestCaseAlpha",
        issue_branch="master",
        issue_status="open",
        issue_created_at="2026-04-09 08:00:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=66982,
        case_name="TestCaseBeta",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-09 08:10:00",
        issue_closed_at="2026-04-12 09:00:00",
    )
    _insert_job_state(
        sqlite_engine,
        job_name="ci-sync-builds",
        last_status="succeeded",
        last_succeeded_at="2026-04-12T09:45:00Z",
    )
    _insert_job_state(
        sqlite_engine,
        job_name="ci-refresh-build-derived",
        last_status="running",
        last_succeeded_at="2026-04-12T09:30:00Z",
    )

    monkeypatch.setattr(
        "ci_dashboard.api.queries.status.utcnow",
        lambda: datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc),
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    with TestClient(app) as client:
        yield client


def test_frontend_serves_static_file_within_dist(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    app_root = repo_root / "ci-dashboard"
    dist_dir = app_root / "web" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist_dir / "dashboard.txt").write_text("dashboard-ok", encoding="utf-8")

    fake_main = app_root / "src" / "ci_dashboard" / "api" / "main.py"
    fake_main.parent.mkdir(parents=True)
    fake_main.write_text("# test\n", encoding="utf-8")
    monkeypatch.setattr("ci_dashboard.api.main.__file__", str(fake_main))

    test_app = create_app()
    with TestClient(test_app) as client:
        response = client.get("/dashboard.txt")

    assert response.status_code == 200
    assert response.text == "dashboard-ok"


def test_frontend_blocks_path_traversal_outside_dist(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    app_root = repo_root / "ci-dashboard"
    dist_dir = app_root / "web" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>spa-shell</html>", encoding="utf-8")
    (app_root / "secret.txt").write_text("top-secret", encoding="utf-8")

    fake_main = app_root / "src" / "ci_dashboard" / "api" / "main.py"
    fake_main.parent.mkdir(parents=True)
    fake_main.write_text("# test\n", encoding="utf-8")
    monkeypatch.setattr("ci_dashboard.api.main.__file__", str(fake_main))

    test_app = create_app()
    with TestClient(test_app) as client:
        response = client.get("/..%2Fsecret.txt")

    assert response.status_code == 200
    assert response.text == "<html>spa-shell</html>"
    assert response.text != "top-secret"
    app.dependency_overrides.clear()


def test_frontend_uses_configured_static_dir(tmp_path: Path, monkeypatch) -> None:
    dist_dir = tmp_path / "custom-dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>configured-spa</html>", encoding="utf-8")
    (dist_dir / "configured.txt").write_text("configured-ok", encoding="utf-8")

    monkeypatch.setenv("CI_DASHBOARD_STATIC_DIR", str(dist_dir))

    test_app = create_app()
    with TestClient(test_app) as client:
        response = client.get("/configured.txt")

    assert response.status_code == 200
    assert response.text == "configured-ok"


def test_status_and_filter_endpoints(api_client: TestClient, sqlite_engine) -> None:
    freshness = api_client.get("/api/v1/status/freshness")
    assert freshness.status_code == 200
    freshness_body = freshness.json()
    assert freshness_body["generated_at"] == "2026-04-12T10:00:00Z"
    assert [job["job_name"] for job in freshness_body["jobs"]] == [
        "ci-refresh-build-derived",
        "ci-sync-builds",
    ]
    assert freshness_body["jobs"][0]["lag_minutes"] == 30
    assert freshness_body["jobs"][1]["lag_minutes"] == 15

    repos = api_client.get("/api/v1/filters/repos")
    assert repos.status_code == 200
    assert repos.json()["items"] == [
        {"value": "pingcap/tidb", "label": "pingcap/tidb"},
        {"value": "pingcap/tiflash", "label": "pingcap/tiflash"},
    ]

    branches = api_client.get("/api/v1/filters/branches", params={"repo": "pingcap/tidb"})
    assert branches.status_code == 200
    assert branches.json()["items"] == [
        {"value": "main", "label": "main"},
        {"value": "master", "label": "master"},
    ]

    _insert_build(
        sqlite_engine,
        source_prow_row_id=900,
        source_prow_job_id="job-900",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="pingcap-integration-test",
        state="success",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-12 09:00:00",
        queue_wait_seconds=20,
        run_seconds=120,
        total_seconds=140,
        pr_number=120,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=901,
        source_prow_job_id="job-901",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="tikv-copr-unit",
        state="success",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-12 09:10:00",
        queue_wait_seconds=25,
        run_seconds=180,
        total_seconds=205,
        pr_number=121,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=902,
        source_prow_job_id="job-902",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="tidbcloud-smoke-suite",
        state="success",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-12 09:20:00",
        queue_wait_seconds=15,
        run_seconds=240,
        total_seconds=255,
        pr_number=122,
    )
    _insert_build(
        sqlite_engine,
        source_prow_row_id=903,
        source_prow_job_id="job-903",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="misc-nightly-job",
        state="success",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-12 09:30:00",
        queue_wait_seconds=10,
        run_seconds=60,
        total_seconds=70,
        pr_number=123,
    )

    jobs = api_client.get(
        "/api/v1/filters/jobs",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-12",
            "end_date": "2026-04-12",
        },
    )
    assert jobs.status_code == 200
    assert jobs.json()["items"] == [
        {"value": "pingcap-integration-test", "label": "pingcap-integration-test"},
        {"value": "tidbcloud-smoke-suite", "label": "tidbcloud-smoke-suite"},
        {"value": "tikv-copr-unit", "label": "tikv-copr-unit"},
    ]

    cloud_phases = api_client.get("/api/v1/filters/cloud-phases")
    assert cloud_phases.status_code == 200
    assert cloud_phases.json()["items"] == [
        {"value": "GCP", "label": "GCP"},
        {"value": "IDC", "label": "IDC"},
    ]


def test_flaky_trend_and_composition(api_client: TestClient) -> None:
    trend = api_client.get(
        "/api/v1/flaky/trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert trend.status_code == 200
    trend_body = trend.json()
    series = {item["key"]: item for item in trend_body["series"]}
    assert series["flaky_rate_pct"]["points"] == [["2026-04-10", 50.0], ["2026-04-11", 50.0]]
    assert series["flaky_rate_pct"]["axis"] == "right"
    assert series["total_failure_like_count"]["points"] == [["2026-04-10", 2], ["2026-04-11", 2]]
    assert series["total_failure_like_count"]["axis"] == "left"
    assert trend_body["meta"]["repo"] == "pingcap/tidb"
    assert trend_body["meta"]["branch"] == "master"

    composition = api_client.get(
        "/api/v1/flaky/composition",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert composition.status_code == 200
    composition_body = composition.json()
    composition_series = {item["key"]: item for item in composition_body["series"]}
    assert composition_series["flaky_rate_pct"]["points"] == [["2026-04-10", 50.0], ["2026-04-11", 50.0]]
    assert composition_series["flaky_rate_pct"]["axis"] == "right"
    assert composition_series["retry_loop_rate_pct"]["points"] == [["2026-04-10", 50.0], ["2026-04-11", 0.0]]
    assert composition_series["retry_loop_rate_pct"]["axis"] == "right"
    assert composition_series["noisy_rate_pct"]["points"] == [["2026-04-10", 100.0], ["2026-04-11", 50.0]]
    assert composition_series["noisy_rate_pct"]["axis"] == "right"
    assert composition_series["total_failure_like_count"]["axis"] == "left"

    distinct_counts = api_client.get(
        "/api/v1/flaky/distinct-case-counts",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert distinct_counts.status_code == 200
    distinct_counts_body = distinct_counts.json()
    assert distinct_counts_body["weeks"] == ["2026-04-06"]
    assert distinct_counts_body["rows"] == [{"branch": "master", "values": [2]}]

    issue_weekly_rates = api_client.get(
        "/api/v1/flaky/issue-weekly-rates",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert issue_weekly_rates.status_code == 200
    issue_weekly_rates_body = issue_weekly_rates.json()
    assert issue_weekly_rates_body["weeks"] == ["2026-04-06"]
    assert [row["case_name"] for row in issue_weekly_rates_body["rows"]] == [
        "TestCaseAlpha",
        "TestCaseBeta",
    ]
    assert issue_weekly_rates_body["rows"][0]["cells"] == ["100.00% (1/1)"]
    assert issue_weekly_rates_body["rows"][1]["cells"] == ["100.00% (1/1)"]
    assert issue_weekly_rates_body["trend"]["series"] == [
        {
            "key": "issue_filtered_flaky_rate_pct",
            "type": "line",
            "points": [["2026-04-06", 100.0]],
        }
    ]

    issue_weekly_rates_open = api_client.get(
        "/api/v1/flaky/issue-weekly-rates",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "issue_status": "open",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert issue_weekly_rates_open.status_code == 200
    assert [row["case_name"] for row in issue_weekly_rates_open.json()["rows"]] == [
        "TestCaseAlpha",
    ]


def test_flaky_top_jobs_and_period_comparison(api_client: TestClient) -> None:
    top_jobs = api_client.get(
        "/api/v1/flaky/top-jobs",
        params={"repo": "pingcap/tidb", "branch": "master", "limit": 2},
    )
    assert top_jobs.status_code == 200
    top_jobs_body = top_jobs.json()
    assert [item["name"] for item in top_jobs_body["items"]] == ["job-a", "job-c"]
    assert top_jobs_body["items"][0]["value"] == 100.0
    assert top_jobs_body["items"][0]["noisy_build_count"] == 2
    assert top_jobs_body["items"][0]["noisy_rate_pct"] == 100.0
    assert top_jobs_body["meta"]["limit"] == 2
    assert top_jobs_body["meta"]["value_key"] == "noisy_rate_pct"

    period_comparison = api_client.get(
        "/api/v1/flaky/period-comparison",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "period_a_start": "2026-04-10",
            "period_a_end": "2026-04-10",
            "period_b_start": "2026-04-11",
            "period_b_end": "2026-04-11",
        },
    )
    assert period_comparison.status_code == 200
    groups = {
        group["name"]: group["values"]
        for group in period_comparison.json()["groups"]
    }
    assert groups["period_a"] == {
        "total_build_count": 3,
        "failure_like_build_count": 2,
        "flaky_build_count": 1,
        "retry_loop_build_count": 1,
        "noisy_build_count": 2,
        "total_pr_count": 2,
        "affected_pr_count": 1,
        "flaky_rate_pct": 50.0,
        "retry_loop_rate_pct": 50.0,
        "noisy_rate_pct": 100.0,
        "affected_pr_rate_pct": 50.0,
    }
    assert groups["period_b"] == {
        "total_build_count": 2,
        "failure_like_build_count": 2,
        "flaky_build_count": 1,
        "retry_loop_build_count": 0,
        "noisy_build_count": 1,
        "total_pr_count": 1,
        "affected_pr_count": 1,
        "flaky_rate_pct": 50.0,
        "retry_loop_rate_pct": 0.0,
        "noisy_rate_pct": 50.0,
        "affected_pr_rate_pct": 100.0,
    }


def test_flaky_case_flow_v2_two_week_confirmation(sqlite_engine, api_client: TestClient) -> None:
    weeks = [
        "2026-03-02",
        "2026-03-09",
        "2026-03-16",
        "2026-03-23",
        "2026-03-30",
    ]
    build_keys = [
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-v2-flow/1001/",
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-v2-flow/1002/",
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-v2-flow/1003/",
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-v2-flow/1004/",
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-v2-flow/1005/",
    ]

    for index, week in enumerate(weeks, start=1):
        _insert_build(
            sqlite_engine,
            source_prow_row_id=2000 + index,
            source_prow_job_id=f"v2-flow-{index}",
            repo_full_name="pingcap/tidb",
            target_branch="release-v2-test",
            base_ref="release-v2-test",
            job_name="job-v2-flow",
            state="failure",
            cloud_phase="GCP",
            is_flaky=0,
            is_retry_loop=0,
            failure_category=None,
            start_time=f"{week} 08:00:00",
            pr_number=8800 + index,
            normalized_build_url=build_keys[index - 1],
            build_id=f"build-v2-{index}",
        )
        _insert_pr_event(
            sqlite_engine,
            repo="pingcap/tidb",
            pr_number=8800 + index,
            target_branch="release-v2-test",
            event_key=f"v2-pr-{index}",
            event_time=f"{week} 07:55:00",
        )

    # Case A: present in week1 + week2 -> "new" confirmed at week2.
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="release-v2-test",
        case_name="CaseA",
        build_url=build_keys[0],
        flaky=1,
        report_time="2026-03-02 08:10:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="release-v2-test",
        case_name="CaseA",
        build_url=build_keys[1],
        flaky=1,
        report_time="2026-03-09 08:10:00",
    )

    # Case C: present in week2 + week3 then absent in week4 + week5 -> "resolved" at week5.
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="release-v2-test",
        case_name="CaseC",
        build_url=build_keys[1],
        flaky=1,
        report_time="2026-03-09 08:11:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="release-v2-test",
        case_name="CaseC",
        build_url=build_keys[2],
        flaky=1,
        report_time="2026-03-16 08:11:00",
    )

    response = api_client.get(
        "/api/v1/flaky/case-flow-v2",
        params={
            "repo": "pingcap/tidb",
            "branch": "release-v2-test",
            "start_date": "2026-03-02",
            "end_date": "2026-03-30",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["weeks"] == weeks

    series = {item["key"]: item for item in body["series"]}
    assert series["new_case_count"]["points"] == [
        ["2026-03-02", 0],
        ["2026-03-09", 1],
        ["2026-03-16", 1],
        ["2026-03-23", 0],
        ["2026-03-30", 0],
    ]
    assert series["resolved_case_count"]["points"] == [
        ["2026-03-02", 0],
        ["2026-03-09", 0],
        ["2026-03-16", 0],
        ["2026-03-23", 1],
        ["2026-03-30", 1],
    ]

    summary_by_week = {row["week_start"]: row for row in body["summary"]}
    assert summary_by_week["2026-03-09"]["net_case_count"] == 1
    assert summary_by_week["2026-03-30"]["net_case_count"] == -1


def test_case_tables_exclude_cross_cloud_and_stale_build_key_collisions(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        source_prow_row_id=900,
        source_prow_job_id="live-build",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="collision-job",
        state="success",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-10 12:00:00",
        pr_number=900,
        normalized_build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/944/",
        build_id="prow-live",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=900,
        target_branch="master",
        event_key="pr-900-master",
        event_time="2026-04-10 11:55:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestLive",
        build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/944/",
        flaky=1,
        report_time="2026-04-10 12:10:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70001,
        case_name="TestLive",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-09 08:00:00",
        issue_closed_at="2026-04-10 20:00:00",
    )

    _insert_build(
        sqlite_engine,
        source_prow_row_id=901,
        source_prow_job_id="ghost-cloud-build",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="collision-job",
        state="success",
        cloud_phase="GCP",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-10 13:00:00",
        pr_number=901,
        normalized_build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/943/",
        build_id="prow-ghost-cloud",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=901,
        target_branch="master",
        event_key="pr-901-master",
        event_time="2026-04-10 12:55:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestGhostCloud",
        build_url="https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/943/",
        flaky=1,
        report_time="2025-07-25 04:01:13",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70002,
        case_name="TestGhostCloud",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-09 08:10:00",
        issue_closed_at="2026-04-10 20:10:00",
    )

    _insert_build(
        sqlite_engine,
        source_prow_row_id=902,
        source_prow_job_id="ghost-time-build",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="collision-job",
        state="success",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-04-10 14:00:00",
        pr_number=902,
        normalized_build_url="https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/945/",
        build_id="prow-ghost-time",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=902,
        target_branch="master",
        event_key="pr-902-master",
        event_time="2026-04-10 13:55:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestGhostTime",
        build_url="https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/pull_unit_test_next_gen/945/",
        flaky=1,
        report_time="2025-07-24 04:01:13",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70003,
        case_name="TestGhostTime",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-09 08:20:00",
        issue_closed_at="2026-04-10 20:20:00",
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            distinct_counts = client.get(
                "/api/v1/flaky/distinct-case-counts",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-10",
                    "end_date": "2026-04-11",
                },
            )
            assert distinct_counts.status_code == 200
            assert distinct_counts.json()["rows"] == [{"branch": "master", "values": [1]}]

            issue_weekly_rates = client.get(
                "/api/v1/flaky/issue-weekly-rates",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "issue_status": "closed",
                    "start_date": "2026-04-10",
                    "end_date": "2026-04-11",
                },
            )
            assert issue_weekly_rates.status_code == 200
            rows = {
                row["case_name"]: row
                for row in issue_weekly_rates.json()["rows"]
            }
            assert rows["TestLive"]["cells"] == ["50.00% (1/2)"]
            assert rows["TestGhostCloud"]["cells"] == ["0.00% (0/0)"]
            assert rows["TestGhostTime"]["cells"] == ["0.00% (0/0)"]
            assert issue_weekly_rates.json()["trend"]["series"] == [
                {
                    "key": "issue_filtered_flaky_rate_pct",
                    "type": "line",
                    "points": [["2026-04-06", 50.0]],
                }
            ]
    finally:
        app.dependency_overrides.clear()


def test_distinct_case_counts_match_legacy_do_host_case_runs(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        source_prow_row_id=910,
        source_prow_job_id="legacy-idc-build",
        repo_full_name="pingcap/tidb",
        target_branch="master",
        base_ref="master",
        job_name="legacy-job",
        state="success",
        cloud_phase="IDC",
        is_flaky=0,
        is_retry_loop=0,
        failure_category=None,
        start_time="2026-03-23 15:08:10",
        pr_number=910,
        normalized_build_url="https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/54603/",
        build_id="prow-legacy-idc",
    )
    _insert_pr_event(
        sqlite_engine,
        repo="pingcap/tidb",
        pr_number=910,
        target_branch="master",
        event_key="pr-910-master",
        event_time="2026-03-23 15:05:00",
    )
    _insert_problem_case_run(
        sqlite_engine,
        repo="pingcap/tidb",
        branch="master",
        case_name="TestLegacyDoHost",
        build_url="https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/54603/",
        flaky=1,
        report_time="2026-03-23 15:20:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70010,
        case_name="TestLegacyDoHost",
        issue_branch="master",
        issue_status="open",
        issue_created_at="2026-03-23 16:00:00",
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            distinct_counts = client.get(
                "/api/v1/flaky/distinct-case-counts",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-03-23",
                    "end_date": "2026-03-23",
                },
            )
            assert distinct_counts.status_code == 200
            assert distinct_counts.json()["rows"] == [{"branch": "master", "values": [1]}]

            issue_weekly_rates = client.get(
                "/api/v1/flaky/issue-weekly-rates",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-03-23",
                    "end_date": "2026-03-23",
                },
            )
            assert issue_weekly_rates.status_code == 200
            rows = {row["case_name"]: row for row in issue_weekly_rates.json()["rows"]}
            assert rows["TestLegacyDoHost"]["cells"] == ["100.00% (1/1)"]
    finally:
        app.dependency_overrides.clear()


def test_flaky_validation_errors(api_client: TestClient) -> None:
    invalid_granularity = api_client.get(
        "/api/v1/flaky/trend",
        params={"granularity": "quarter"},
    )
    assert invalid_granularity.status_code == 400
    assert invalid_granularity.json()["detail"] == "granularity must be one of: day, week, month"

    invalid_range = api_client.get(
        "/api/v1/flaky/composition",
        params={"start_date": "2026-04-12", "end_date": "2026-04-11"},
    )
    assert invalid_range.status_code == 400
    assert invalid_range.json()["detail"] == "start_date must be on or before end_date"

    invalid_limit = api_client.get(
        "/api/v1/flaky/top-jobs",
        params={"limit": 0},
    )
    assert invalid_limit.status_code == 400
    assert invalid_limit.json()["detail"] == "limit must be positive"

    invalid_issue_status = api_client.get(
        "/api/v1/flaky/issue-weekly-rates",
        params={"issue_status": "reopened"},
    )
    assert invalid_issue_status.status_code == 400
    assert invalid_issue_status.json()["detail"] == "issue_status must be one of: open, closed"


def test_build_routes(api_client: TestClient) -> None:
    outcome_trend = api_client.get(
        "/api/v1/builds/outcome-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert outcome_trend.status_code == 200
    outcome_body = outcome_trend.json()
    outcome_series = {item["key"]: item for item in outcome_body["series"]}
    assert outcome_series["total_count"]["points"] == [["2026-04-10", 3], ["2026-04-11", 2]]
    assert outcome_series["success_rate_pct"]["axis"] == "right"
    assert outcome_series["success_rate_pct"]["points"] == [["2026-04-10", 33.33], ["2026-04-11", 0.0]]
    assert outcome_body["meta"]["summary"] == {
        "total_count": 5,
        "success_count": 1,
        "failure_count": 4,
        "success_rate_pct": 20.0,
    }

    outcome_multi_job = api_client.get(
        "/api/v1/builds/outcome-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "job_name": "job-a,job-b",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert outcome_multi_job.status_code == 200
    outcome_multi_job_summary = outcome_multi_job.json()["meta"]["summary"]
    assert outcome_multi_job_summary == {
        "total_count": 4,
        "success_count": 1,
        "failure_count": 3,
        "success_rate_pct": 25.0,
    }

    duration_trend = api_client.get(
        "/api/v1/builds/duration-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert duration_trend.status_code == 200
    duration_body = duration_trend.json()
    duration_series = {item["key"]: item for item in duration_body["series"]}
    assert duration_series["queue_avg_s"]["points"] == [["2026-04-10", 120], ["2026-04-11", 0]]
    assert duration_series["run_avg_s"]["points"] == [["2026-04-10", 600], ["2026-04-11", 0]]
    assert duration_series["total_avg_s"]["points"] == [["2026-04-10", 720], ["2026-04-11", 0]]
    assert duration_body["meta"]["summary"] == {
        "queue_avg_s": 120,
        "run_avg_s": 600,
        "total_avg_s": 720,
    }

    cloud_comparison = api_client.get(
        "/api/v1/builds/cloud-comparison",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert cloud_comparison.status_code == 200
    cloud_groups = {item["name"]: item["metrics"] for item in cloud_comparison.json()["groups"]}
    assert cloud_groups["GCP"]["total_builds"] == 4
    assert cloud_groups["IDC"]["total_builds"] == 1
    assert cloud_groups["IDC"]["success_rate_pct"] == 0.0
    assert cloud_groups["GCP"]["queue_avg_s"] == 120
    assert cloud_groups["GCP"]["run_avg_s"] == 600
    assert cloud_groups["GCP"]["total_avg_s"] == 720
    assert cloud_groups["IDC"]["queue_avg_s"] == 0

    migration_runtime = api_client.get(
        "/api/v1/builds/migration-runtime-comparison",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-15",
        },
    )
    assert migration_runtime.status_code == 200
    migration_body = migration_runtime.json()
    assert migration_body["meta"]["anchor_end_date"] == "2026-04-15"
    assert migration_body["meta"]["window_days"] == 14
    assert migration_body["meta"]["min_success_runs_each_side"] == 5
    assert migration_body["meta"]["improved_limit"] == 10
    assert migration_body["improved"] == [
        {
            "job_name": "job-fast",
            "normalized_job_path": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-fast/",
            "idc_baseline_avg_run_s": 600,
            "gcp_recent_avg_run_s": 300,
            "delta_run_s": -300,
            "delta_pct": -50.0,
            "idc_success_count": 5,
            "gcp_success_count": 5,
            "first_gcp_success_at": "2026-03-01T09:00:00Z",
        }
    ]
    assert migration_body["regressed"] == [
        {
            "job_name": "job-slow",
            "normalized_job_path": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/job-slow/",
            "idc_baseline_avg_run_s": 200,
            "gcp_recent_avg_run_s": 500,
            "delta_run_s": 300,
            "delta_pct": 150.0,
            "idc_success_count": 5,
            "gcp_success_count": 5,
            "first_gcp_success_at": "2026-03-01T10:00:00Z",
        }
    ]


def test_failure_routes(api_client: TestClient) -> None:
    category_trend = api_client.get(
        "/api/v1/failures/category-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert category_trend.status_code == 200
    category_series = {item["key"]: item for item in category_trend.json()["series"]}
    assert category_series["FLAKY_TEST"]["points"] == [["2026-04-10", 2], ["2026-04-11", 1]]
    assert category_series["UNCLASSIFIED"]["points"] == [["2026-04-10", 0], ["2026-04-11", 1]]

    category_share = api_client.get(
        "/api/v1/failures/category-share",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert category_share.status_code == 200
    category_share_body = category_share.json()
    assert category_share_body["categories"] == ["FLAKY_TEST", "UNCLASSIFIED"]
    share_groups = {item["name"]: item["values"] for item in category_share_body["groups"]}
    assert share_groups["GCP"] == [2, 1]
    assert share_groups["IDC"] == [1, 0]


def test_page_routes(api_client: TestClient) -> None:
    overview = api_client.get(
        "/api/v1/pages/overview",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert overview.status_code == 200
    overview_body = overview.json()
    assert overview_body["scope"]["repo"] == "pingcap/tidb"
    assert len(overview_body["repos"]["items"]) == 2
    assert overview_body["top_noisy_jobs"]["meta"]["limit"] == 5
    overview_groups = {
        group["name"]: group["values"]
        for group in overview_body["period_comparison"]["groups"]
    }
    assert overview_groups["period_a"]["total_build_count"] == 5
    assert overview_groups["period_a"]["noisy_build_count"] == 3
    assert overview_groups["period_b"]["total_build_count"] == 0

    ci_status = api_client.get(
        "/api/v1/pages/ci-status",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-15",
        },
    )
    assert ci_status.status_code == 200
    build_trend_body = ci_status.json()
    assert build_trend_body["scope"]["branch"] == "master"
    error_catalog_counts = {
        item["name"]: item["value"] for item in build_trend_body["error_catalog_share"]["items"]
    }
    assert error_catalog_counts == {"BUILD": 2, "INFRA": 2}
    error_details = build_trend_body["error_catalog_share"]["l2_details"]
    assert {item["name"]: item["value"] for item in error_details["BUILD"]} == {
        "COMPILE": 2,
    }
    assert {item["name"]: item["value"] for item in error_details["INFRA"]} == {
        "DISK_FULL": 1,
        "JENKINS": 1,
    }
    assert build_trend_body["cloud_posture_trend"]["meta"]["bucket_granularity"] == "week"
    assert build_trend_body["cloud_posture_trend"]["series"] == []
    assert build_trend_body["longest_avg_success_jobs"]["items"] == [
        {
            "name": "job-b",
            "value": 600,
            "total_build_count": 2,
            "success_build_count": 1,
            "success_rate_pct": 50.0,
            "job_url": "https://prow.tidb.net/jenkins/job/job-4/",
        }
    ]
    assert build_trend_body["lowest_success_rate_jobs"]["items"] == [
        {
            "name": "job-a",
            "value": 0.0,
            "total_build_count": 2,
            "success_build_count": 0,
            "success_avg_run_s": 0,
            "job_url": "https://prow.tidb.net/jenkins/job/job-2/",
        },
        {
            "name": "job-c",
            "value": 0.0,
            "total_build_count": 1,
            "success_build_count": 0,
            "success_avg_run_s": 0,
            "job_url": "https://prow.tidb.net/jenkins/job/job-5/",
        },
        {
            "name": "job-b",
            "value": 50.0,
            "total_build_count": 2,
            "success_build_count": 1,
            "success_avg_run_s": 600,
            "job_url": "https://prow.tidb.net/jenkins/job/job-4/",
        },
    ]
    assert build_trend_body["migration_runtime_comparison"]["improved"][0]["job_name"] == "job-fast"
    assert build_trend_body["migration_runtime_comparison"]["regressed"][0]["job_name"] == "job-slow"
    assert build_trend_body["migration_runtime_comparison"]["meta"]["improved_limit"] == 10

    build_trend_all = api_client.get(
        "/api/v1/pages/ci-status",
        params={"start_date": "2026-04-10", "end_date": "2026-04-11"},
    )
    assert build_trend_all.status_code == 200
    build_trend_all_body = build_trend_all.json()
    build_count_breakdown = build_trend_all_body["build_count_breakdown_trend"]
    assert build_count_breakdown["repo"]["items"] == [
        {"name": "pingcap/tidb", "value": 6, "share_pct": 85.71},
        {"name": "pingcap/tiflash", "value": 1, "share_pct": 14.29},
    ]
    repo_series = {
        item["label"]: item["points"]
        for item in build_count_breakdown["repo"]["series"]
    }
    assert repo_series["pingcap/tidb"] == [["2026-04-10", 3], ["2026-04-11", 3]]
    assert repo_series["pingcap/tiflash"] == [["2026-04-10", 0], ["2026-04-11", 1]]
    assert build_count_breakdown["branch"]["items"] == [
        {"name": "master", "value": 5, "share_pct": 71.43},
        {"name": "main", "value": 1, "share_pct": 14.29},
        {"name": "release-8.5", "value": 1, "share_pct": 14.29},
    ]
    branch_series = {
        item["label"]: item["points"]
        for item in build_count_breakdown["branch"]["series"]
    }
    assert branch_series["master"] == [["2026-04-10", 3], ["2026-04-11", 2]]
    assert branch_series["main"] == [["2026-04-10", 0], ["2026-04-11", 1]]
    assert branch_series["release-8.5"] == [["2026-04-10", 0], ["2026-04-11", 1]]
    assert build_count_breakdown["author"]["items"] == [
        {"name": "alice", "value": 5, "share_pct": 71.43},
        {"name": "bob", "value": 1, "share_pct": 14.29},
        {"name": "carol", "value": 1, "share_pct": 14.29},
    ]
    author_series = {
        item["label"]: item["points"]
        for item in build_count_breakdown["author"]["series"]
    }
    assert author_series["alice"] == [["2026-04-10", 3], ["2026-04-11", 2]]
    assert author_series["bob"] == [["2026-04-10", 0], ["2026-04-11", 1]]
    assert author_series["carol"] == [["2026-04-10", 0], ["2026-04-11", 1]]
    cloud_repo_share = {
        item["cloud_phase"]: item for item in build_trend_all_body["cloud_repo_share"]["clouds"]
    }
    assert cloud_repo_share["GCP"]["total_builds"] == 4
    assert cloud_repo_share["GCP"]["items"] == [
        {
            "name": "pingcap/tidb",
            "value": 4,
            "share_pct": 100.0,
            "branches": [{"name": "master", "value": 4, "share_pct": 100.0}],
        }
    ]
    assert cloud_repo_share["IDC"]["total_builds"] == 3
    assert cloud_repo_share["IDC"]["items"] == [
        {
            "name": "pingcap/tidb",
            "value": 2,
            "share_pct": 66.67,
            "branches": [
                {"name": "main", "value": 1, "share_pct": 50.0},
                {"name": "master", "value": 1, "share_pct": 50.0},
            ],
        },
        {
            "name": "pingcap/tiflash",
            "value": 1,
            "share_pct": 33.33,
            "branches": [{"name": "release-8.5", "value": 1, "share_pct": 100.0}],
        },
    ]

    build_trend_repo_filtered = api_client.get(
        "/api/v1/pages/ci-status",
        params={
            "repo": "pingcap/tidb",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert build_trend_repo_filtered.status_code == 200
    build_trend_repo_filtered_body = build_trend_repo_filtered.json()
    cloud_repo_share_with_repo_filter = {
        item["cloud_phase"]: item
        for item in build_trend_repo_filtered_body["cloud_repo_share"]["clouds"]
    }
    assert cloud_repo_share_with_repo_filter["GCP"]["total_builds"] == 4
    assert cloud_repo_share_with_repo_filter["IDC"]["total_builds"] == 3
    assert [item["name"] for item in cloud_repo_share_with_repo_filter["IDC"]["items"]] == [
        "pingcap/tidb",
        "pingcap/tiflash",
    ]

    build_trend_cloud_filtered = api_client.get(
        "/api/v1/pages/ci-status",
        params={
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
            "cloud_phase": "GCP",
        },
    )
    assert build_trend_cloud_filtered.status_code == 200
    build_trend_cloud_filtered_body = build_trend_cloud_filtered.json()
    cloud_repo_share_with_cloud_filter = {
        item["cloud_phase"]: item
        for item in build_trend_cloud_filtered_body["cloud_repo_share"]["clouds"]
    }
    assert build_trend_cloud_filtered_body["cloud_posture_trend"]["series"] == []

    assert cloud_repo_share_with_cloud_filter["GCP"]["total_builds"] == 4
    assert cloud_repo_share_with_cloud_filter["IDC"]["total_builds"] == 3

    flaky = api_client.get(
        "/api/v1/pages/flaky",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert flaky.status_code == 200
    flaky_body = flaky.json()
    assert flaky_body["top_jobs"]["meta"]["limit"] == 8
    assert flaky_body["distinct_flaky_case_counts"]["weeks"] == ["2026-04-06"]
    assert flaky_body["distinct_flaky_case_counts"]["rows"] == [
        {"branch": "master", "values": [2]}
    ]
    assert flaky_body["issue_filtered_weekly_trend"]["series"] == [
        {
            "key": "issue_filtered_flaky_rate_pct",
            "type": "line",
            "points": [["2026-04-06", 100.0]],
        }
    ]
    assert flaky_body["bucketed_flaky_rate"]["series"] == [
        {
            "key": "flaky_rate_pct",
            "label": "Flaky rate",
            "type": "line",
            "points": [["2026-04-06", 50.0]],
        }
    ]
    assert flaky_body["bucketed_flaky_rate"]["rows"] == [
        {
            "time": "2026-04-06",
            "flaky_rate_pct": 50.0,
            "time_to_time_pct": None,
        }
    ]
    assert flaky_body["bucketed_flaky_rate"]["meta"]["requested_granularity"] == "day"
    assert flaky_body["bucketed_flaky_rate"]["meta"]["effective_granularity"] == "week"
    assert flaky_body["issue_case_weekly_rates"]["rows"][0]["display_name"] == "[master] TestCaseAlpha"
    assert flaky_body["issue_case_weekly_rates"]["rows"][0]["cells"] == ["100.00% (1/1)"]
    assert flaky_body["issue_case_weekly_rates"]["rows"][1]["cells"] == ["100.00% (1/1)"]
    failure_series = {
        item["key"]: item
        for item in flaky_body["failure_category_trend"]["series"]
    }
    assert failure_series["FLAKY_TEST"]["points"] == [["2026-04-10", 2], ["2026-04-11", 1]]
    period_groups = {
        group["name"]: group["values"]
        for group in flaky_body["period_comparison"]["groups"]
    }
    assert period_groups["period_a"]["total_pr_count"] == 3
    assert period_groups["period_a"]["affected_pr_count"] == 2
    assert period_groups["period_a"]["affected_pr_rate_pct"] == 66.67

    flaky_with_open_issues = api_client.get(
        "/api/v1/pages/flaky",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "issue_status": "open",
            "start_date": "2026-04-10",
            "end_date": "2026-04-11",
        },
    )
    assert flaky_with_open_issues.status_code == 200
    flaky_with_open_issues_body = flaky_with_open_issues.json()
    assert [row["case_name"] for row in flaky_with_open_issues_body["issue_case_weekly_rates"]["rows"]] == [
        "TestCaseAlpha"
    ]
    assert flaky_with_open_issues_body["bucketed_flaky_rate"] == flaky_body["bucketed_flaky_rate"]
    assert flaky_with_open_issues_body["trend"] == flaky_body["trend"]
    assert flaky_with_open_issues_body["composition"] == flaky_body["composition"]
    assert flaky_with_open_issues_body["top_jobs"] == flaky_body["top_jobs"]
    assert flaky_with_open_issues_body["failure_category_share"] == flaky_body["failure_category_share"]
    assert flaky_with_open_issues_body["failure_category_trend"] == flaky_body["failure_category_trend"]
    assert flaky_with_open_issues_body["period_comparison"] == flaky_body["period_comparison"]


def test_cost_page_route(sqlite_engine, api_client: TestClient) -> None:
    _insert_roster_group(
        sqlite_engine,
        group_id=100,
        lark_group_id="eng",
        name="Engineering Group",
        path="/100/",
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=110,
        lark_group_id="database",
        name="Database",
        parent_id=100,
        path="/100/110/",
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=111,
        lark_group_id="tidb",
        name="TiDB",
        parent_id=110,
        path="/100/110/111/",
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=120,
        lark_group_id="infra",
        name="Infra",
        parent_id=100,
        path="/100/120/",
    )

    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-04-06",
        repo="tidb",
        group_id=111,
        net_cost=10,
        effective_cost=12,
        list_cost=15,
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-04-07",
        repo="tidb",
        group_id=110,
        net_cost=20,
        effective_cost=22,
        list_cost=25,
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-04-13",
        repo="tiflash",
        group_id=120,
        net_cost=30,
        effective_cost=32,
        list_cost=35,
    )

    response = api_client.get(
        "/api/v1/pages/cost",
        params={
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "granularity": "week",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["granularity"] == "week"
    assert body["cost_trend"]["meta"]["summary"]["net_cost"] == 60.0
    assert body["cost_trend"]["meta"]["summary"]["matched_resource_pct"] == 100.0
    assert body["cost_trend"]["meta"]["summary"]["matched_resource_cost"] == 75.0
    assert body["cost_trend"]["meta"]["summary"]["total_resource_cost"] == 75.0
    trend_series = {series["key"]: series["points"] for series in body["cost_trend"]["series"]}
    assert trend_series["net_cost"] == [
        ["2026-03-30", 0.0],
        ["2026-04-06", 30.0],
        ["2026-04-13", 30.0],
        ["2026-04-20", 0.0],
        ["2026-04-27", 0.0],
    ]

    stack = body["repo_group_stack"]
    assert [item["name"] for item in stack["items"]] == [
        "tidb",
        "tiflash",
    ]
    stack_series = {series["label"]: series["points"] for series in stack["series"]}
    assert stack_series["tidb"] == [
        ["2026-03-30", 0.0],
        ["2026-04-06", 40.0],
        ["2026-04-13", 0.0],
        ["2026-04-20", 0.0],
        ["2026-04-27", 0.0],
    ]
    assert stack_series["tiflash"] == [
        ["2026-03-30", 0.0],
        ["2026-04-06", 0.0],
        ["2026-04-13", 35.0],
        ["2026-04-20", 0.0],
        ["2026-04-27", 0.0],
    ]

    engineering_share = body["engineering_group_share"]
    level1 = {item["name"]: item for item in engineering_share["level1"]["items"]}
    assert level1["Database"]["value"] == 40.0
    assert level1["Infra"]["value"] == 35.0
    assert level1["Database"]["share_pct"] == 53.33
    level2 = {item["name"]: item for item in engineering_share["level2"]["items"]}
    assert level2["TiDB"]["value"] == 15.0


def test_cost_page_unmatched_resources(sqlite_engine, api_client: TestClient) -> None:
    _insert_roster_group(
        sqlite_engine,
        group_id=100,
        lark_group_id="eng",
        name="Engineering Group",
        path="/100/",
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=110,
        lark_group_id="database",
        name="Database",
        parent_id=100,
        path="/100/110/",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-10",
        repo="tidb",
        group_id=110,
        net_cost=0,
        effective_cost=90,
        list_cost=120,
        resource_name=None,
        author=None,
        owner=None,
        attribution_key=None,
        attribution_source="missing_author",
        attribution_status="unmatched",
        employee_id=None,
        usage_seconds=172800,
    )
    _insert_cost_unmatched_resource(
        sqlite_engine,
        usage_date="2026-05-10",
        repo="tidb",
        resource_name="projects/pingcap-prod/zones/us-central1-a/instances/tidb-ci-runner-1",
        namespace="kube:unallocated",
        author=None,
        list_cost=120,
        effective_cost=90,
        net_cost=0,
        usage_seconds=172800,
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-01",
        repo="platform",
        group_id=110,
        net_cost=0,
        effective_cost=20,
        list_cost=30,
        resource_name=None,
        author=None,
        owner=None,
        attribution_key=None,
        attribution_source="missing_author",
        attribution_status="unattributed",
        employee_id=None,
        usage_seconds=86400,
        dimension_hash="cost-log-bucket-start",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-31",
        repo="platform",
        group_id=110,
        net_cost=0,
        effective_cost=20,
        list_cost=30,
        resource_name=None,
        author=None,
        owner=None,
        attribution_key=None,
        attribution_source="missing_author",
        attribution_status="unattributed",
        employee_id=None,
        usage_seconds=86400,
        dimension_hash="cost-log-bucket-end",
    )
    _insert_cost_unmatched_resource(
        sqlite_engine,
        usage_date="2026-05-01",
        repo="platform",
        resource_name="//logging.googleapis.com/projects/890604261603/locations/global/buckets/_Default",
        namespace=None,
        author=None,
        list_cost=30,
        effective_cost=20,
        net_cost=0,
        usage_seconds=86400,
        service_name="Cloud Logging",
        sku_name="Vended Logs Storage",
        source_row_hash="resource-log-bucket-start",
    )
    _insert_cost_unmatched_resource(
        sqlite_engine,
        usage_date="2026-05-31",
        repo="platform",
        resource_name="//logging.googleapis.com/projects/890604261603/locations/global/buckets/_Default",
        namespace=None,
        author=None,
        list_cost=30,
        effective_cost=20,
        net_cost=0,
        usage_seconds=86400,
        service_name="Cloud Logging",
        sku_name="Vended Logs Storage",
        source_row_hash="resource-log-bucket-end",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-11",
        repo="ticdc",
        group_id=110,
        net_cost=0,
        effective_cost=70,
        list_cost=95,
        resource_name="projects/pingcap-prod/zones/us-central1-b/instances/ticdc-batch-2",
        author="bob",
        owner=None,
        attribution_key="repo:ticdc",
        attribution_source="missing_owner",
        attribution_status="partial",
        employee_id=None,
        usage_seconds=21600,
    )
    _insert_cost_unmatched_resource(
        sqlite_engine,
        usage_date="2026-05-11",
        repo="ticdc",
        resource_name="projects/pingcap-prod/zones/us-central1-b/instances/ticdc-batch-2",
        namespace="jenkins-tidb",
        author="bob",
        list_cost=95,
        effective_cost=70,
        net_cost=0,
        usage_seconds=21600,
    )

    response = api_client.get(
        "/api/v1/pages/cost-unmatched-resources",
        params={
            "start_date": "2026-05-01",
            "end_date": "2026-05-31",
            "granularity": "week",
        },
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["resource_name"] for item in items] == [
        "projects/pingcap-prod/zones/us-central1-a/instances/tidb-ci-runner-1",
        "//logging.googleapis.com/projects/890604261603/locations/global/buckets/_Default",
    ]
    assert items[0]["list_cost"] == 120.0
    assert items[0]["first_seen_date"] == "2026-05-10"
    assert items[0]["last_seen_date"] == "2026-05-10"
    assert items[0]["observed_days"] == 1
    assert items[0]["labels"] == "org=pingcap, repo=tidb"
    assert items[0]["allocation_buckets"] == "kube:unallocated"
    assert items[0]["attribution_source"] == "missing_author"
    assert items[1]["list_cost"] == 60.0
    assert items[1]["first_seen_date"] == "2026-05-01"
    assert items[1]["last_seen_date"] == "2026-05-31"
    assert items[1]["observed_days"] is None

    bounded_response = api_client.get(
        "/api/v1/pages/cost-unmatched-resources",
        params={
            "start_date": "2026-01-01",
            "end_date": "2026-05-31",
            "granularity": "week",
        },
    )

    assert bounded_response.status_code == 200
    bounded_meta = bounded_response.json()["meta"]
    assert bounded_meta["start_date"] == "2026-05-01"
    assert bounded_meta["requested_start_date"] == "2026-01-01"
    assert bounded_meta["window_limited"] is True
    assert bounded_meta["max_window_days"] == 31


def test_cost_page_supporting_routes(sqlite_engine, api_client: TestClient) -> None:
    _insert_roster_group(
        sqlite_engine,
        group_id=100,
        lark_group_id="eng",
        name="Engineering Group",
        path="/100/",
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=110,
        lark_group_id="database",
        name="Database",
        parent_id=100,
        path="/100/110/",
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=111,
        lark_group_id="tidb",
        name="TiDB",
        parent_id=110,
        path="/100/110/111/",
    )

    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-02",
        repo="",
        group_id=111,
        net_cost=8,
        effective_cost=9,
        list_cost=10,
    )

    params = {
        "start_date": "2026-05-01",
        "end_date": "2026-05-31",
        "granularity": "day",
    }

    trend_response = api_client.get("/api/v1/pages/cost-trend", params=params)
    assert trend_response.status_code == 200
    trend_body = trend_response.json()
    assert trend_body["meta"]["granularity"] == "week"
    assert trend_body["meta"]["summary"]["list_cost"] == 10.0
    assert trend_body["meta"]["summary"]["net_cost"] == 8.0
    trend_series = {series["key"]: series["points"] for series in trend_body["series"]}
    assert trend_series["list_cost"] == [
        ["2026-04-27", 10.0],
        ["2026-05-04", 0.0],
        ["2026-05-11", 0.0],
        ["2026-05-18", 0.0],
        ["2026-05-25", 0.0],
    ]

    stack_response = api_client.get("/api/v1/pages/cost-repo-group-stack", params=params)
    assert stack_response.status_code == 200
    stack_body = stack_response.json()
    assert stack_body["meta"]["granularity"] == "week"
    assert stack_body["meta"]["group_by"] == "repo"
    assert stack_body["items"] == [{"name": "(no repo)", "value": 10.0}]
    assert stack_body["series"] == [
        {
            "key": "repo__no_repo",
            "label": "(no repo)",
            "type": "bar",
            "points": [
                ["2026-04-27", 10.0],
                ["2026-05-04", 0.0],
                ["2026-05-11", 0.0],
                ["2026-05-18", 0.0],
                ["2026-05-25", 0.0],
            ],
        }
    ]

    author_stack_response = api_client.get(
        "/api/v1/pages/cost-repo-group-stack",
        params={**params, "group_by": "author"},
    )
    assert author_stack_response.status_code == 200
    author_stack_body = author_stack_response.json()
    assert author_stack_body["meta"]["group_by"] == "author"
    assert author_stack_body["items"] == [{"name": "alice", "value": 10.0}]
    assert author_stack_body["series"][0]["key"] == "author__0"
    assert author_stack_body["series"][0]["label"] == "alice"
    assert author_stack_body["series"][0]["points"] == [
        ["2026-04-27", 10.0],
        ["2026-05-04", 0.0],
        ["2026-05-11", 0.0],
        ["2026-05-18", 0.0],
        ["2026-05-25", 0.0],
    ]

    team_stack_response = api_client.get(
        "/api/v1/pages/cost-repo-group-stack",
        params={**params, "group_by": "team"},
    )
    assert team_stack_response.status_code == 200
    team_stack_body = team_stack_response.json()
    assert team_stack_body["meta"]["group_by"] == "team"
    assert team_stack_body["items"] == [{"name": "TiDB", "value": 10.0}]
    assert team_stack_body["series"][0]["key"] == "team__0"
    assert team_stack_body["series"][0]["label"] == "TiDB"
    assert team_stack_body["series"][0]["points"] == [
        ["2026-04-27", 10.0],
        ["2026-05-04", 0.0],
        ["2026-05-11", 0.0],
        ["2026-05-18", 0.0],
        ["2026-05-25", 0.0],
    ]

    branch_stack_response = api_client.get(
        "/api/v1/pages/cost-repo-group-stack",
        params={**params, "group_by": "target_branch"},
    )
    assert branch_stack_response.status_code == 200
    branch_stack_body = branch_stack_response.json()
    assert branch_stack_body["meta"]["group_by"] == "target_branch"
    assert branch_stack_body["items"] == [{"name": "(no target branch)", "value": 10.0}]
    assert branch_stack_body["series"][0]["key"] == "target_branch__no_target_branch"

    share_response = api_client.get("/api/v1/pages/cost-engineering-group-share", params=params)
    assert share_response.status_code == 200
    share_body = share_response.json()
    assert share_body["level1"]["meta"]["granularity"] == "week"
    assert share_body["level1"]["items"] == [
        {
            "name": "Database",
            "value": 10.0,
            "share_pct": 100.0,
            "interactive": False,
        }
    ]
    assert share_body["level2"]["items"] == [
        {
            "name": "TiDB",
            "value": 10.0,
            "share_pct": 100.0,
            "interactive": False,
        }
    ]


def test_cost_routes_filter_by_target_branch(sqlite_engine, api_client: TestClient) -> None:
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-02",
        repo="tidb",
        group_id=111,
        target_branch="master",
        net_cost=8,
        list_cost=10,
        dimension_hash="master-cost",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-02",
        repo="tidb",
        group_id=111,
        target_branch="release-8.5",
        net_cost=80,
        list_cost=100,
        dimension_hash="release-cost",
    )

    params = {
        "start_date": "2026-05-01",
        "end_date": "2026-05-31",
        "branch": "master",
    }
    trend_response = api_client.get("/api/v1/pages/cost-trend", params=params)
    assert trend_response.status_code == 200
    trend_body = trend_response.json()
    assert trend_body["meta"]["branch"] == "master"
    assert trend_body["meta"]["summary"]["list_cost"] == 10.0
    assert trend_body["meta"]["summary"]["net_cost"] == 8.0

    stack_response = api_client.get(
        "/api/v1/pages/cost-repo-group-stack",
        params={**params, "group_by": "target_branch"},
    )
    assert stack_response.status_code == 200
    stack_body = stack_response.json()
    assert stack_body["items"] == [{"name": "master", "value": 10.0}]


def test_cost_source_filter_and_sources_route(sqlite_engine, api_client: TestClient) -> None:
    _insert_cost_source(
        sqlite_engine,
        vendor="gcp",
        account_id="pingcap-testing-account",
        display_name="pingcap-testing-account",
        billing_account_id="01D088-8F9CF2-8AF1C6",
    )
    _insert_cost_source(
        sqlite_engine,
        vendor="gcp",
        account_id="qa-infra-dev",
        display_name="qa-infra-dev",
        billing_account_id="01D088-8F9CF2-8AF1C6",
    )
    _insert_cost_source(
        sqlite_engine,
        vendor="aws",
        account_id="946646677266",
        display_name="qa-infra-dev",
        billing_account_id="317766989874",
    )
    _insert_cost_source(
        sqlite_engine,
        vendor="gcp",
        account_id="disabled-project",
        display_name="disabled-project",
        is_active=0,
    )
    _insert_cost_budget(
        sqlite_engine,
        vendor="aws",
        account_id="946646677266",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=36000.0,
        budget_name="qa-infra-dev 2026",
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=100,
        lark_group_id="eng",
        name="Engineering Group",
        path="/100/",
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=110,
        lark_group_id="database",
        name="Database",
        parent_id=100,
        path="/100/110/",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-02",
        vendor="gcp",
        account_id="pingcap-testing-account",
        repo="tidb",
        group_id=110,
        net_cost=8,
        effective_cost=9,
        list_cost=10,
        dimension_hash="gcp-main",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-02",
        vendor="gcp",
        account_id="qa-infra-dev",
        repo="tidb",
        group_id=110,
        net_cost=18,
        effective_cost=19,
        list_cost=20,
        dimension_hash="gcp-qa",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-02",
        vendor="aws",
        account_id="946646677266",
        repo="tidb",
        group_id=110,
        net_cost=28,
        effective_cost=29,
        list_cost=30,
        dimension_hash="aws-qa",
    )

    sources_response = api_client.get("/api/v1/pages/cost-sources")

    assert sources_response.status_code == 200
    assert sources_response.json()["items"] == [
        {
            "value": "aws:946646677266",
            "label": "aws / 946646677266",
            "vendor": "aws",
            "account_id": "946646677266",
            "display_name": "qa-infra-dev",
        },
        {
            "value": "gcp:pingcap-testing-account",
            "label": "gcp / pingcap-testing-account",
            "vendor": "gcp",
            "account_id": "pingcap-testing-account",
            "display_name": "pingcap-testing-account",
        },
        {
            "value": "gcp:qa-infra-dev",
            "label": "gcp / qa-infra-dev",
            "vendor": "gcp",
            "account_id": "qa-infra-dev",
            "display_name": "qa-infra-dev",
        },
    ]

    trend_response = api_client.get(
        "/api/v1/pages/cost-trend",
        params={
            "start_date": "2026-05-01",
            "end_date": "2026-05-31",
            "granularity": "day",
            "cost_source": "aws:946646677266",
        },
    )

    assert trend_response.status_code == 200
    trend_body = trend_response.json()
    assert trend_body["meta"]["cost_vendor"] == "aws"
    assert trend_body["meta"]["cost_account_id"] == "946646677266"
    assert trend_body["meta"]["cost_source"] == "aws:946646677266"
    assert trend_body["meta"]["annual_budgets"] == {"2026": 36000.0}
    assert trend_body["meta"]["summary"]["list_cost"] == 30.0
    assert trend_body["meta"]["summary"]["net_cost"] == 28.0
    assert {series["key"]: series["points"] for series in trend_body["series"]}["list_cost"] == [
        ["2026-04-27", 30.0],
        ["2026-05-04", 0.0],
        ["2026-05-11", 0.0],
        ["2026-05-18", 0.0],
        ["2026-05-25", 0.0],
    ]


def test_cost_weekly_account_summaries_route(
    sqlite_engine,
    api_client: TestClient,
) -> None:
    for vendor, account_id, display_name, annual_budget in [
        ("aws", "946646677266", "qa-infra-dev", 5200.0),
        ("gcp", "pingcap-testing-account", "pingcap-testing-account", 10400.0),
        ("gcp", "qa-infra-dev", "qa-infra-dev", 2600.0),
    ]:
        _insert_cost_source(
            sqlite_engine,
            vendor=vendor,
            account_id=account_id,
            display_name=display_name,
        )
        _insert_cost_budget(
            sqlite_engine,
            vendor=vendor,
            account_id=account_id,
            period_start_date="2026-01-01",
            period_end_date="2026-12-31",
            budget_amount=annual_budget,
        )

    for usage_date, vendor, account_id, net_cost in [
        ("2026-05-28", "aws", "946646677266", 80),
        ("2026-06-04", "aws", "946646677266", 120),
        ("2026-05-28", "gcp", "pingcap-testing-account", 160),
        ("2026-06-04", "gcp", "pingcap-testing-account", 240),
        ("2026-05-28", "gcp", "qa-infra-dev", 50),
        ("2026-06-04", "gcp", "qa-infra-dev", 40),
    ]:
        _insert_cost_attribution(
            sqlite_engine,
            usage_date=usage_date,
            vendor=vendor,
            account_id=account_id,
            repo="tidb",
            group_id=None,
            net_cost=net_cost,
            list_cost=net_cost,
            dimension_hash=f"{usage_date}-{vendor}-{account_id}",
        )

    response = api_client.get(
        "/api/v1/pages/cost-weekly-account-summaries",
        params={
            "start_date": "2026-06-01",
            "end_date": "2026-06-07",
            "granularity": "week",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["previous_scope"]["start_date"] == "2026-05-25"
    assert body["previous_scope"]["end_date"] == "2026-05-31"
    assert body["items"] == [
        {
            "cost_source": "aws:946646677266",
            "vendor": "aws",
            "account_id": "946646677266",
            "display_name": "qa-infra-dev",
            "net_cost": 120.0,
            "previous_net_cost": 80.0,
            "net_cost_wow_pct": 50.0,
            "annual_budget": 5200.0,
            "weekly_budget": 100.0,
            "over_budget": True,
        },
        {
            "cost_source": "gcp:pingcap-testing-account",
            "vendor": "gcp",
            "account_id": "pingcap-testing-account",
            "display_name": "pingcap-testing-account",
            "net_cost": 240.0,
            "previous_net_cost": 160.0,
            "net_cost_wow_pct": 50.0,
            "annual_budget": 10400.0,
            "weekly_budget": 200.0,
            "over_budget": True,
        },
        {
            "cost_source": "gcp:qa-infra-dev",
            "vendor": "gcp",
            "account_id": "qa-infra-dev",
            "display_name": "qa-infra-dev",
            "net_cost": 40.0,
            "previous_net_cost": 50.0,
            "net_cost_wow_pct": -20.0,
            "annual_budget": 2600.0,
            "weekly_budget": 50.0,
            "over_budget": False,
        },
    ]


def test_cost_weekly_account_summaries_filter_by_target_branch(
    sqlite_engine,
    api_client: TestClient,
) -> None:
    _insert_cost_source(
        sqlite_engine,
        vendor="gcp",
        account_id="pingcap-testing-account",
        display_name="pingcap-testing-account",
    )
    for usage_date, target_branch, net_cost in [
        ("2026-05-28", "master", 80),
        ("2026-06-04", "master", 120),
        ("2026-05-28", "release-8.5", 800),
        ("2026-06-04", "release-8.5", 1200),
    ]:
        _insert_cost_attribution(
            sqlite_engine,
            usage_date=usage_date,
            vendor="gcp",
            account_id="pingcap-testing-account",
            repo="tidb",
            group_id=None,
            target_branch=target_branch,
            net_cost=net_cost,
            list_cost=net_cost,
            dimension_hash=f"{usage_date}-{target_branch}",
        )

    response = api_client.get(
        "/api/v1/pages/cost-weekly-account-summaries",
        params={
            "start_date": "2026-06-01",
            "end_date": "2026-06-07",
            "branch": "master",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["branch"] == "master"
    assert body["items"][0]["net_cost"] == 120.0
    assert body["items"][0]["previous_net_cost"] == 80.0
    assert body["items"][0]["net_cost_wow_pct"] == 50.0


def test_cost_weekly_overview_route(
    sqlite_engine,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cost_queries, "_today", lambda: date(2026, 5, 20))
    _insert_cost_budget(
        sqlite_engine,
        vendor="gcp",
        account_id="pingcap-testing-account",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=207600.0,
        budget_name="PingCAP Testing 2026",
    )

    _insert_roster_group(
        sqlite_engine,
        group_id=100,
        lark_group_id="eng",
        name="Engineering Group",
        path="/100/",
    )
    for group_id, lark_group_id, name, parent_id, path in [
        (110, "database", "Database", 100, "/100/110/"),
        (111, "tidb", "TiDB", 110, "/100/110/111/"),
        (112, "storage", "Storage", 110, "/100/110/112/"),
        (120, "infra", "Infra", 100, "/100/120/"),
        (121, "platform", "Platform", 120, "/100/120/121/"),
        (122, "data", "Data", 120, "/100/120/122/"),
        (123, "tiny-parent", "Tiny Parent", 100, "/100/123/"),
        (124, "tiny", "Tiny", 123, "/100/123/124/"),
    ]:
        _insert_roster_group(
            sqlite_engine,
            group_id=group_id,
            lark_group_id=lark_group_id,
            name=name,
            parent_id=parent_id,
            path=path,
        )

    for service_name, group_id, list_cost, net_cost in [
        ("Compute Engine", 111, 490, 330),
        ("Cloud Storage", 112, 250, 175),
        ("Cloud SQL", 121, 100, 70),
        ("BigQuery", 122, 80, 56),
        ("Networking", 111, 70, 49),
        ("Cloud Monitoring", 111, 20, 0),
        ("Cloud Logging", 124, 10, 20),
    ]:
        _insert_cost_attribution(
            sqlite_engine,
            usage_date="2026-05-18",
            repo="tidb",
            group_id=group_id,
            list_cost=list_cost,
            net_cost=net_cost,
            service_name=service_name,
            dimension_hash=f"current-{service_name}",
        )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-12",
        repo="tidb",
        group_id=111,
        list_cost=500,
        net_cost=350,
        service_name="Compute Engine",
        dimension_hash="previous-compute",
    )

    response = api_client.get(
        "/api/v1/pages/cost-weekly-overview",
        params={
            "start_date": "2026-05-16",
            "end_date": "2026-05-22",
            "granularity": "day",
            "cost_source": "gcp:pingcap-testing-account",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["start_date"] == "2026-05-16"
    assert body["scope"]["end_date"] == "2026-05-22"
    assert body["scope"]["granularity"] == "week"
    assert body["scope"]["cost_source"] == "gcp:pingcap-testing-account"
    assert body["previous_scope"]["start_date"] == "2026-05-09"
    assert body["previous_scope"]["end_date"] == "2026-05-15"
    assert body["summary"] == {
        "list_cost": 1020.0,
        "net_cost": 700.0,
        "previous_list_cost": 500.0,
        "previous_net_cost": 350.0,
        "list_cost_wow_pct": 104.0,
        "net_cost_wow_pct": 100.0,
    }
    assert body["budget_health"] == {
        "metric_key": "net_cost",
        "annual_budget": 207600.0,
        "budget_to_date": 77352.33,
        "current_cost": 350.0,
        "through_date": "2026-05-16",
        "days_elapsed": 136,
        "days_in_year": 365,
        "days_remaining": 229,
        "annual_budget_pct": 0.17,
        "budget_to_date_pct": 0.45,
        "variance": -77002.33,
        "variance_pct": -99.55,
        "recent_window_days": 14,
        "recent_window_cost": 350.0,
        "recent_daily_cost": 25.0,
        "forecast_remaining_cost": 5725.0,
        "forecast_total_cost": 6075.0,
        "forecast_budget_pct": 2.93,
        "forecast_variance": -201525.0,
        "forecast_variance_pct": -97.07,
        "status": "healthy",
        "status_label": "Healthy",
    }
    assert [item["name"] for item in body["service_share"]["items"]] == [
        "Compute Engine",
        "Cloud Storage",
        "Cloud SQL",
        "BigQuery",
        "Networking",
        "Cloud Monitoring",
        "Others",
    ]
    service_others = body["service_share"]["items"][-1]
    assert service_others["name"] == "Others"
    assert service_others["value"] == 10.0
    assert service_others["share_pct"] == pytest.approx(0.98)
    assert service_others["interactive"] is False
    assert body["service_share"]["meta"]["min_share_pct"] == 1.0
    assert body["service_share"]["meta"]["total_list_cost"] == 1020.0
    level2_names = [item["name"] for item in body["level2_share"]["items"]]
    assert level2_names == ["TiDB", "Storage", "Platform", "Data", "Others"]
    assert "Tiny" not in level2_names
    assert body["level2_share"]["items"][-1]["value"] == 10.0


def test_cost_weekly_overview_previous_window_filters_by_target_branch(
    sqlite_engine,
    api_client: TestClient,
) -> None:
    for usage_date, target_branch, list_cost, net_cost in [
        ("2026-05-18", "master", 10, 8),
        ("2026-05-12", "master", 5, 4),
        ("2026-05-18", "release-8.5", 100, 80),
        ("2026-05-12", "release-8.5", 500, 400),
    ]:
        _insert_cost_attribution(
            sqlite_engine,
            usage_date=usage_date,
            repo="tidb",
            group_id=None,
            target_branch=target_branch,
            list_cost=list_cost,
            net_cost=net_cost,
            dimension_hash=f"{usage_date}-{target_branch}",
        )

    response = api_client.get(
        "/api/v1/pages/cost-weekly-overview",
        params={
            "start_date": "2026-05-16",
            "end_date": "2026-05-22",
            "branch": "master",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["branch"] == "master"
    assert body["summary"]["list_cost"] == 10.0
    assert body["summary"]["net_cost"] == 8.0
    assert body["summary"]["previous_list_cost"] == 5.0
    assert body["summary"]["previous_net_cost"] == 4.0


def test_cost_query_page_helpers_cover_parallel_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    assert page_queries._get_previous_date_range(CommonFilters()) == (None, None)
    assert page_queries._get_previous_date_range(
        CommonFilters(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 7),
        )
    ) == (date(2026, 4, 24), date(2026, 4, 30))

    class _ImmediateFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class _InlineExecutor:
        def __init__(self, *, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, task):
            return _ImmediateFuture(task())

    monkeypatch.setattr(page_queries, "ThreadPoolExecutor", _InlineExecutor)

    engine = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    resolved = page_queries._resolve_page_sections(
        engine,
        {
            "alpha": lambda: 1,
            "beta": lambda: 2,
        },
    )
    assert resolved == {
        "alpha": 1,
        "beta": 2,
    }
    assert page_queries._normalize_cost_filters(
        CommonFilters(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
            granularity="day",
            branch="master",
            cost_vendor="aws",
            cost_account_id="946646677266",
        )
    ) == CommonFilters(
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 31),
        branch="master",
        granularity="week",
        cost_vendor="aws",
        cost_account_id="946646677266",
    )


def test_get_cost_page_parallelizes_for_non_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_granularities: list[str | None] = []

    def _capture(name: str):
        def _inner(_engine, filters: CommonFilters):
            captured_granularities.append(filters.granularity)
            return {"name": name, "granularity": filters.granularity}

        return _inner

    class _ImmediateFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class _InlineExecutor:
        def __init__(self, *, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, task, *args):
            return _ImmediateFuture(task(*args))

    monkeypatch.setattr(cost_queries, "ThreadPoolExecutor", _InlineExecutor)
    monkeypatch.setattr(cost_queries, "get_cost_trend", _capture("trend"))
    monkeypatch.setattr(cost_queries, "get_repo_group_cost_stack", _capture("stack"))
    monkeypatch.setattr(cost_queries, "get_engineering_group_share", _capture("share"))

    result = cost_queries.get_cost_page(
        SimpleNamespace(dialect=SimpleNamespace(name="mysql")),
        CommonFilters(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
            granularity="day",
            cost_vendor="aws",
            cost_account_id="946646677266",
        ),
    )

    assert result["scope"]["start_date"] == "2026-05-01"
    assert result["scope"]["end_date"] == "2026-05-31"
    assert result["scope"]["granularity"] == "week"
    assert result["scope"]["cost_vendor"] == "aws"
    assert result["scope"]["cost_account_id"] == "946646677266"
    assert result["scope"]["job_names"] == []
    assert result["cost_trend"] == {"name": "trend", "granularity": "week"}
    assert result["repo_group_stack"] == {"name": "stack", "granularity": "week"}
    assert result["engineering_group_share"] == {"name": "share", "granularity": "week"}
    assert captured_granularities == ["week", "week", "week"]


def test_get_weekly_overview_parallelizes_for_non_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ImmediateFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class _InlineExecutor:
        def __init__(self, *, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, task, *args, **kwargs):
            return _ImmediateFuture(task(*args, **kwargs))

    class _Begin:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Engine:
        dialect = SimpleNamespace(name="mysql")

        def begin(self):
            return _Begin()

    def _summary(_connection, filters: CommonFilters):
        if filters.start_date == date(2026, 5, 16):
            return {"list_cost": 120.0, "net_cost": 90.0}
        return {"list_cost": 100.0, "net_cost": 80.0}

    monkeypatch.setattr(cost_queries, "ThreadPoolExecutor", _InlineExecutor)
    monkeypatch.setattr(cost_queries, "_cost_summary", _summary)
    monkeypatch.setattr(
        cost_queries,
        "_get_budget_health_snapshot",
        lambda _engine, _filters: {
            "status": "healthy",
            "current_cost": 90.0,
            "budget_to_date": 120.0,
            "annual_budget": 240.0,
        },
    )
    monkeypatch.setattr(
        cost_queries,
        "_service_share_by_threshold",
        lambda _connection, _filters, *, min_share_pct: {
            "items": [{"name": "Compute Engine", "value": 120.0, "share_pct": 100.0}],
            "meta": {"min_share_pct": min_share_pct, "total_list_cost": 120.0},
        },
    )
    monkeypatch.setattr(
        cost_queries,
        "_engineering_share_by_level_threshold",
        lambda _connection, _filters, *, level, min_share_pct: {
            "items": [{"name": "TiDB", "value": 120.0, "share_pct": 100.0}],
            "meta": {"level": level, "min_share_pct": min_share_pct, "total_list_cost": 120.0},
        },
    )

    result = cost_queries.get_weekly_overview(
        _Engine(),
        CommonFilters(
            start_date=date(2026, 5, 16),
            end_date=date(2026, 5, 22),
            granularity="day",
        ),
    )

    assert result["summary"]["list_cost_wow_pct"] == 20.0
    assert result["summary"]["net_cost_wow_pct"] == 12.5
    assert result["budget_health"]["status"] == "healthy"
    assert result["service_share"]["items"][0]["name"] == "Compute Engine"
    assert result["level2_share"]["items"][0]["name"] == "TiDB"


def test_cost_query_helpers_cover_edge_cases(sqlite_engine) -> None:
    empty_share = cost_queries.get_engineering_group_share(
        sqlite_engine,
        CommonFilters(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
            granularity="month",
        ),
    )
    assert empty_share["level1"]["items"] == []
    assert empty_share["level2"]["items"] == []
    assert empty_share["level1"]["meta"]["group_name"] == "Engineering Group"
    assert empty_share["level2"]["meta"]["group_name"] == "Engineering Group"
    assert empty_share["level1"]["meta"]["job_names"] == []
    assert empty_share["level2"]["meta"]["job_names"] == []
    assert empty_share["level1"]["meta"]["granularity"] == "month"
    assert empty_share["level2"]["meta"]["granularity"] == "month"
    assert (
        cost_queries._like_prefix_expr(
            SimpleNamespace(dialect=SimpleNamespace(name="mysql")),
            "child.path",
            "parent.path",
        )
        == "child.path LIKE CONCAT(parent.path, '%')"
    )
    assert cost_queries._observed_days("bad-date", "2026-05-03") is None
    assert (
        cost_queries._observed_days(
            "2026-05-01",
            "2026-05-03",
            window_start=date(2026, 5, 1),
            window_end=date(2026, 5, 31),
        )
        is None
    )
    assert (
        cost_queries._observed_days(
            "2026-05-02",
            "2026-05-03",
            window_start=date(2026, 5, 1),
            window_end=date(2026, 5, 31),
        )
        == 2
    )
    assert cost_queries._parse_date("not-a-date") is None
    assert cost_queries._bucket_starts(
        CommonFilters(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
            granularity="week",
        ),
        [],
    ) == [
        "2026-04-27",
        "2026-05-04",
        "2026-05-11",
        "2026-05-18",
        "2026-05-25",
    ]
    assert cost_queries._bucket_starts(
        CommonFilters(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 7, 2),
            granularity="month",
        ),
        [],
    ) == [
        "2026-05-01",
        "2026-06-01",
        "2026-07-01",
    ]
    assert cost_queries._cost_stack_key("repo", "(no repo)", 0) == "repo__no_repo"
    assert cost_queries._cost_stack_key("repo", "repo-1", 0) != cost_queries._cost_stack_key(
        "repo",
        "repo.1",
        1,
    )
    assert cost_queries._cost_stack_key("author", "(unknown author)", 0) == "author__unknown_author"
    assert cost_queries._cost_stack_key("team", "(no team)", 0) == "team__no_team"


def test_budget_health_snapshot_marks_warning_when_over_pace(
    sqlite_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cost_queries, "_today", lambda: date(2026, 1, 10))
    _insert_cost_budget(
        sqlite_engine,
        vendor="gcp",
        account_id="pingcap-testing-account",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=100.0,
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=100,
        lark_group_id="eng",
        name="Engineering Group",
        path="/100/",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-01-05",
        vendor="gcp",
        account_id="pingcap-testing-account",
        repo="tidb",
        group_id=100,
        net_cost=10,
        effective_cost=10,
        list_cost=10,
        dimension_hash="budget-health-warning",
    )

    with sqlite_engine.begin() as connection:
        snapshot = cost_queries._budget_health_snapshot(
            connection,
            CommonFilters(
                cost_vendor="gcp",
                cost_account_id="pingcap-testing-account",
                granularity="week",
            ),
        )

    assert snapshot == {
        "metric_key": "net_cost",
        "annual_budget": 100.0,
        "budget_to_date": 1.64,
        "current_cost": 10.0,
        "through_date": "2026-01-06",
        "days_elapsed": 6,
        "days_in_year": 365,
        "days_remaining": 359,
        "annual_budget_pct": 10.0,
        "budget_to_date_pct": 609.76,
        "variance": 8.36,
        "variance_pct": 509.76,
        "recent_window_days": 6,
        "recent_window_cost": 10.0,
        "recent_daily_cost": 1.67,
        "forecast_remaining_cost": 599.53,
        "forecast_total_cost": 609.53,
        "forecast_budget_pct": 609.53,
        "forecast_variance": 509.53,
        "forecast_variance_pct": 509.53,
        "status": "warning",
        "status_label": "Warning",
    }


def test_budget_health_snapshot_marks_warning_when_forecast_exceeds_budget_even_if_current_is_under_pace(
    sqlite_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cost_queries, "_today", lambda: date(2026, 6, 30))
    _insert_cost_budget(
        sqlite_engine,
        vendor="gcp",
        account_id="pingcap-testing-account",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=100.0,
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=100,
        lark_group_id="eng",
        name="Engineering Group",
        path="/100/",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-01-05",
        vendor="gcp",
        account_id="pingcap-testing-account",
        repo="tidb",
        group_id=100,
        net_cost=15,
        effective_cost=15,
        list_cost=15,
        dimension_hash="budget-health-forecast-early",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-06-20",
        vendor="gcp",
        account_id="pingcap-testing-account",
        repo="tidb",
        group_id=100,
        net_cost=30,
        effective_cost=30,
        list_cost=30,
        dimension_hash="budget-health-forecast-recent",
    )

    with sqlite_engine.begin() as connection:
        snapshot = cost_queries._budget_health_snapshot(
            connection,
            CommonFilters(
                cost_vendor="gcp",
                cost_account_id="pingcap-testing-account",
                granularity="week",
            ),
        )

    assert snapshot["current_cost"] == 45.0
    assert snapshot["through_date"] == "2026-06-26"
    assert snapshot["budget_to_date"] == 48.49
    assert snapshot["variance"] == -3.49
    assert snapshot["recent_window_days"] == 14
    assert snapshot["recent_daily_cost"] == 2.14
    assert snapshot["forecast_total_cost"] == 447.32
    assert snapshot["forecast_variance"] == 347.32
    assert snapshot["status"] == "warning"


def test_annual_budget_for_filters_falls_back_to_summing_partitioned_budget_rows(
    sqlite_engine,
) -> None:
    _insert_cost_budget(
        sqlite_engine,
        vendor="aws",
        account_id="946646677266",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=120.0,
        budget_name="Apps",
        label_filters={"repo": "apps"},
        repo="apps",
    )
    _insert_cost_budget(
        sqlite_engine,
        vendor="aws",
        account_id="946646677266",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=80.0,
        budget_name="Infra",
        label_filters={"repo": "infra"},
        repo="infra",
    )

    with sqlite_engine.begin() as connection:
        budget = cost_queries._annual_budget_for_filters(
            connection,
            CommonFilters(
                cost_vendor="aws",
                cost_account_id="946646677266",
            ),
            year=2026,
        )

    assert budget == 200.0


def test_annual_budget_for_filters_prefers_source_wide_budget_rows(
    sqlite_engine,
) -> None:
    _insert_cost_budget(
        sqlite_engine,
        vendor="gcp",
        account_id="qa-infra-dev",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=300.0,
        budget_name="Whole source",
    )
    _insert_cost_budget(
        sqlite_engine,
        vendor="gcp",
        account_id="qa-infra-dev",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=120.0,
        budget_name="Apps",
        label_filters={"repo": "apps"},
        repo="apps",
    )

    with sqlite_engine.begin() as connection:
        budget = cost_queries._annual_budget_for_filters(
            connection,
            CommonFilters(
                cost_vendor="gcp",
                cost_account_id="qa-infra-dev",
            ),
            year=2026,
        )

    assert budget == 300.0


def test_annual_budget_for_filters_keeps_zero_budget_rows(
    sqlite_engine,
) -> None:
    _insert_cost_budget(
        sqlite_engine,
        vendor="gcp",
        account_id="zero-budget",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=0.0,
        budget_name="Explicit zero",
    )

    with sqlite_engine.begin() as connection:
        budget = cost_queries._annual_budget_for_filters(
            connection,
            CommonFilters(
                cost_vendor="gcp",
                cost_account_id="zero-budget",
            ),
            year=2026,
        )

    assert budget == 0.0


def test_annual_budgets_by_account_preserves_source_wide_and_fallback_rules(
    sqlite_engine,
) -> None:
    _insert_cost_budget(
        sqlite_engine,
        vendor="gcp",
        account_id="qa-infra-dev",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=300.0,
        budget_name="Whole source",
    )
    _insert_cost_budget(
        sqlite_engine,
        vendor="gcp",
        account_id="qa-infra-dev",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=120.0,
        budget_name="Apps",
        label_filters={"repo": "apps"},
        repo="apps",
    )
    _insert_cost_budget(
        sqlite_engine,
        vendor="aws",
        account_id="946646677266",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=80.0,
        budget_name="Infra",
        label_filters={"repo": "infra"},
        repo="infra",
    )
    _insert_cost_budget(
        sqlite_engine,
        vendor="gcp",
        account_id="zero-budget",
        period_start_date="2026-01-01",
        period_end_date="2026-12-31",
        budget_amount=0.0,
        budget_name="Explicit zero",
    )

    with sqlite_engine.begin() as connection:
        budgets = cost_queries._annual_budgets_by_account(
            connection,
            year=2026,
        )

    assert budgets == {
        ("aws", "946646677266"): 80.0,
        ("gcp", "qa-infra-dev"): 300.0,
        ("gcp", "zero-budget"): 0.0,
    }


def test_cost_repo_group_stack_keeps_distinct_repo_keys_on_slug_collisions(sqlite_engine, api_client: TestClient) -> None:
    _insert_roster_group(
        sqlite_engine,
        group_id=100,
        lark_group_id="eng",
        name="Engineering Group",
        path="/100/",
    )
    _insert_roster_group(
        sqlite_engine,
        group_id=110,
        lark_group_id="database",
        name="Database",
        parent_id=100,
        path="/100/110/",
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-05",
        repo="repo-1",
        group_id=110,
        net_cost=5,
        effective_cost=5,
        list_cost=10,
    )
    _insert_cost_attribution(
        sqlite_engine,
        usage_date="2026-05-05",
        repo="repo.1",
        group_id=110,
        net_cost=7,
        effective_cost=7,
        list_cost=20,
    )

    response = api_client.get(
        "/api/v1/pages/cost-repo-group-stack",
        params={
            "start_date": "2026-05-01",
            "end_date": "2026-05-31",
            "granularity": "week",
        },
    )

    assert response.status_code == 200
    series = response.json()["series"]
    assert [item["label"] for item in series] == ["repo.1", "repo-1"]
    assert [item["key"] for item in series] == ["repo__0", "repo__1"]
    assert series[0]["points"] == [
        ["2026-04-27", 0.0],
        ["2026-05-04", 20.0],
        ["2026-05-11", 0.0],
        ["2026-05-18", 0.0],
        ["2026-05-25", 0.0],
    ]
    assert series[1]["points"] == [
        ["2026-04-27", 0.0],
        ["2026-05-04", 10.0],
        ["2026-05-11", 0.0],
        ["2026-05-18", 0.0],
        ["2026-05-25", 0.0],
    ]


def test_migration_fixed_window_comparison_rows(
    sqlite_engine,
    api_client: TestClient,
) -> None:
    fixtures = [
        (2000, "baseline-tidb-success-1", "pingcap/tidb", "success", "IDC", "2025-12-20 01:00:00", 300),
        (2001, "baseline-tidb-success-2", "pingcap/tidb", "success", "IDC", "2026-01-10 01:00:00", 420),
        (2002, "baseline-tidb-failure", "pingcap/tidb", "failure", "IDC", "2026-01-12 01:00:00", 0),
        (2003, "baseline-ticdc-success", "pingcap/ticdc", "success", "IDC", "2025-12-28 02:00:00", 240),
        (2004, "baseline-ticdc-failure", "pingcap/ticdc", "failure", "IDC", "2026-01-05 02:00:00", 0),
        (2010, "recent-tidb-success-1", "pingcap/tidb", "success", "GCP", "2026-04-20 01:00:00", 220),
        (2011, "recent-tidb-success-2", "pingcap/tidb", "success", "GCP", "2026-05-01 01:00:00", 260),
        (2012, "recent-tidb-failure", "pingcap/tidb", "failure", "GCP", "2026-05-03 01:00:00", 0),
        (2013, "recent-ticdc-success", "pingcap/ticdc", "success", "GCP", "2026-04-25 02:00:00", 180),
        (2014, "recent-ticdc-failure", "pingcap/ticdc", "failure", "GCP", "2026-05-05 02:00:00", 0),
    ]
    for source_prow_row_id, source_prow_job_id, repo_full_name, state, cloud_phase, start_time, total_seconds in fixtures:
        _insert_build(
            sqlite_engine,
            source_prow_row_id=source_prow_row_id,
            source_prow_job_id=source_prow_job_id,
            repo_full_name=repo_full_name,
            target_branch="master",
            base_ref="master",
            job_name=f"{repo_full_name.split('/')[-1]}-migration-job",
            state=state,
            cloud_phase=cloud_phase,
            is_flaky=0,
            is_retry_loop=0,
            failure_category=None,
            start_time=start_time,
            run_seconds=max(total_seconds - 20, 0),
            total_seconds=total_seconds,
            pr_number=80000 + source_prow_row_id,
        )

    response = api_client.get(
        "/api/v1/pages/ci-status",
        params={
            "repo": "pingcap/tidb",
            "end_date": "2026-05-15",
        },
    )
    assert response.status_code == 200
    body = response.json()
    comparison = body["migration_fixed_window_comparison"]
    assert comparison["meta"]["baseline_start_date"] == "2025-12-15"
    assert comparison["meta"]["baseline_end_date"] == "2026-01-14"
    assert comparison["meta"]["recent_start_date"] == "2026-04-15"
    assert comparison["meta"]["recent_end_date"] == "2026-05-15"
    assert comparison["meta"]["ignores_repo_filter"] is True

    rows = {row["scope_key"]: row for row in comparison["rows"]}
    assert rows["all_repos"]["baseline"] == {
        "start_date": "2025-12-15",
        "end_date": "2026-01-14",
        "total_build_count": 5,
        "success_count": 3,
        "success_rate_pct": 60.0,
        "success_avg_total_s": 320,
    }
    assert rows["all_repos"]["recent_gcp"] == {
        "start_date": "2026-04-15",
        "end_date": "2026-05-15",
        "total_build_count": 5,
        "success_count": 3,
        "success_rate_pct": 60.0,
        "success_avg_total_s": 220,
    }
    assert rows["tidb"]["baseline"] == {
        "start_date": "2025-12-15",
        "end_date": "2026-01-14",
        "total_build_count": 3,
        "success_count": 2,
        "success_rate_pct": 66.67,
        "success_avg_total_s": 360,
    }
    assert rows["tidb"]["recent_gcp"] == {
        "start_date": "2026-04-15",
        "end_date": "2026-05-15",
        "total_build_count": 3,
        "success_count": 2,
        "success_rate_pct": 66.67,
        "success_avg_total_s": 240,
    }
    assert rows["ticdc"]["baseline"] == {
        "start_date": "2025-12-15",
        "end_date": "2026-01-14",
        "total_build_count": 2,
        "success_count": 1,
        "success_rate_pct": 50.0,
        "success_avg_total_s": 240,
    }
    assert rows["ticdc"]["recent_gcp"] == {
        "start_date": "2026-04-15",
        "end_date": "2026-05-15",
        "total_build_count": 2,
        "success_count": 1,
        "success_rate_pct": 50.0,
        "success_avg_total_s": 180,
    }


def test_ci_status_cloud_migration_summary(
    sqlite_engine,
    api_client: TestClient,
) -> None:
    fixtures = [
        (2200, "migration-share-gcp-1", "GCP", 300),
        (2201, "migration-share-gcp-2", "GCP", 100),
        (2202, "migration-share-idc-1", "IDC", 400),
    ]
    for source_prow_row_id, source_prow_job_id, cloud_phase, total_seconds in fixtures:
        _insert_build(
            sqlite_engine,
            source_prow_row_id=source_prow_row_id,
            source_prow_job_id=source_prow_job_id,
            repo_full_name="pingcap/migration-share",
            target_branch="master",
            base_ref="master",
            job_name="migration-share-job",
            state="success",
            cloud_phase=cloud_phase,
            is_flaky=0,
            is_retry_loop=0,
            failure_category=None,
            start_time="2026-06-03 01:00:00",
            run_seconds=max(total_seconds - 20, 0),
            total_seconds=total_seconds,
            pr_number=82000 + source_prow_row_id,
        )

    response = api_client.get(
        "/api/v1/pages/ci-status",
        params={
            "repo": "pingcap/migration-share",
            "start_date": "2026-06-01",
            "end_date": "2026-06-07",
            "granularity": "week",
        },
    )

    assert response.status_code == 200
    assert response.json()["cloud_migration_summary"] == {
        "gcp_build_count": 2,
        "idc_build_count": 1,
        "total_build_count": 3,
        "gcp_build_share_pct": 66.67,
        "gcp_total_duration_s": 400,
        "idc_total_duration_s": 400,
        "total_duration_s": 800,
        "gcp_duration_share_pct": 50.0,
        "meta": {
            "repo": "pingcap/migration-share",
            "branch": None,
            "job_name": None,
            "job_names": [],
            "cloud_phase": None,
            "issue_status": None,
            "start_date": "2026-06-01",
            "end_date": "2026-06-07",
            "granularity": "week",
            "cost_vendor": None,
            "cost_account_id": None,
            "cost_source": None,
        },
    }


def test_weekly_series_skip_partial_boundary_weeks(api_client: TestClient) -> None:
    outcome_day = api_client.get(
        "/api/v1/builds/outcome-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "granularity": "day",
            "start_date": "2026-03-01",
            "end_date": "2026-04-15",
        },
    )
    assert outcome_day.status_code == 200

    outcome = api_client.get(
        "/api/v1/builds/outcome-trend",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "granularity": "week",
            "start_date": "2026-03-01",
            "end_date": "2026-04-15",
        },
    )
    assert outcome.status_code == 200
    outcome_series = {item["key"]: item for item in outcome.json()["series"]}
    assert outcome_series["total_count"]["points"] == [["2026-03-30", 8], ["2026-04-06", 7]]
    assert outcome_series["success_count"]["points"] == [["2026-03-30", 8], ["2026-04-06", 3]]
    assert outcome_series["failure_count"]["points"] == [["2026-03-30", 0], ["2026-04-06", 4]]
    assert outcome_series["success_rate_pct"]["points"] == [
        ["2026-03-30", 100.0],
        ["2026-04-06", 42.86],
    ]
    assert outcome.json()["meta"]["summary"] == outcome_day.json()["meta"]["summary"]

    ci_status = api_client.get(
        "/api/v1/pages/ci-status",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "granularity": "week",
            "start_date": "2026-03-01",
            "end_date": "2026-04-15",
        },
    )
    assert ci_status.status_code == 200
    cloud_posture_series = {
        item["key"]: item for item in ci_status.json()["cloud_posture_trend"]["series"]
    }
    assert cloud_posture_series["gcp_build_count"]["points"] == [
        ["2026-03-30", 8],
        ["2026-04-06", 6],
    ]
    assert cloud_posture_series["idc_build_count"]["points"] == [
        ["2026-03-30", 0],
        ["2026-04-06", 1],
    ]


def test_flaky_page_bucketed_rate_supports_month_granularity(
    sqlite_engine,
    api_client: TestClient,
) -> None:
    monthly_repo = "pingcap/flaky-monthly"
    for build in [
        (91001, "monthly-1", "2026-03-03T10:00:00Z", 1),
        (91002, "monthly-2", "2026-03-18T10:00:00Z", 0),
        (91003, "monthly-3", "2026-04-02T10:00:00Z", 1),
        (91004, "monthly-4", "2026-04-09T10:00:00Z", 0),
        (91005, "monthly-5", "2026-04-16T10:00:00Z", 0),
        (91006, "monthly-6", "2026-04-24T10:00:00Z", 0),
    ]:
        _insert_build(
            sqlite_engine,
            source_prow_row_id=build[0],
            source_prow_job_id=build[1],
            repo_full_name=monthly_repo,
            target_branch="master",
            base_ref="master",
            job_name="monthly-flaky-job",
            state="failure",
            cloud_phase="GCP",
            is_flaky=build[3],
            is_retry_loop=0,
            failure_category="FLAKY_TEST" if build[3] else "UNCLASSIFIED",
            start_time=build[2],
        )

    response = api_client.get(
        "/api/v1/pages/flaky",
        params={
            "repo": monthly_repo,
            "branch": "master",
            "granularity": "month",
            "start_date": "2026-03-01",
            "end_date": "2026-04-30",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["bucketed_flaky_rate"]["meta"]["requested_granularity"] == "month"
    assert body["bucketed_flaky_rate"]["meta"]["effective_granularity"] == "month"
    assert body["bucketed_flaky_rate"]["series"] == [
        {
            "key": "flaky_rate_pct",
            "label": "Flaky rate",
            "type": "line",
            "points": [["2026-03-01", 50.0], ["2026-04-01", 25.0]],
        }
    ]
    assert body["bucketed_flaky_rate"]["rows"] == [
        {
            "time": "2026-03-01",
            "flaky_rate_pct": 50.0,
            "time_to_time_pct": None,
        },
        {
            "time": "2026-04-01",
            "flaky_rate_pct": 25.0,
            "time_to_time_pct": -25,
        },
    ]


def test_flaky_page_issue_lifecycle_snapshot_ignores_issue_status_filter(
    sqlite_engine,
    api_client: TestClient,
) -> None:
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70001,
        case_name="LifecycleCaseOne",
        issue_branch="master",
        issue_status="open",
        issue_created_at="2026-04-13 10:00:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70002,
        case_name="LifecycleCaseTwo",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-14 10:00:00",
        issue_closed_at="2026-04-16 11:00:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=70003,
        case_name="LifecycleCaseThree",
        issue_branch="master",
        issue_status="open",
        issue_created_at="2026-04-01 10:00:00",
        last_reopened_at="2026-04-17 09:00:00",
        reopen_count=1,
    )

    response = api_client.get(
        "/api/v1/pages/flaky",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "issue_status": "open",
            "start_date": "2026-04-01",
            "end_date": "2026-04-20",
        },
    )
    assert response.status_code == 200
    body = response.json()
    lifecycle = body["issue_lifecycle"]
    assert lifecycle["meta"]["latest_full_week_start"] == "2026-04-13"
    assert lifecycle["meta"]["latest_full_week_end"] == "2026-04-19"
    assert lifecycle["meta"]["ignores_issue_status"] is True
    assert lifecycle["meta"]["ignores_branch"] is True
    assert lifecycle["latest_week_created_count"] == 2
    assert lifecycle["latest_week_created_open_count"] == 1
    assert lifecycle["latest_week_created_closed_count"] == 1
    assert lifecycle["latest_week_closed_count"] == 1
    assert lifecycle["latest_week_reopened_count"] == 1

    weekly_series = {item["key"]: item["points"] for item in body["issue_lifecycle_weekly"]["series"]}
    created_by_week = {week: count for week, count in weekly_series["issue_created_count"]}
    closed_by_week = {week: count for week, count in weekly_series["issue_closed_count"]}
    reopened_by_week = {week: count for week, count in weekly_series["issue_reopened_count"]}
    open_by_week = {week: count for week, count in weekly_series["issue_open_count"]}

    assert created_by_week["2026-03-30"] >= 1
    assert created_by_week["2026-04-13"] >= 2
    assert closed_by_week["2026-04-13"] >= 1
    assert reopened_by_week["2026-04-13"] >= 1
    assert open_by_week["2026-03-30"] >= 1
    assert open_by_week["2026-04-13"] == 3


def test_flaky_page_issue_fix_progress_snapshot(
    sqlite_engine,
    api_client: TestClient,
) -> None:
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=71001,
        case_name="SnapshotCaseOne",
        issue_branch="master",
        issue_status="open",
        issue_created_at="2026-04-05 10:00:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=71002,
        case_name="SnapshotCaseTwo",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-14 09:00:00",
        issue_closed_at="2026-04-24 11:00:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=71003,
        case_name="SnapshotCaseThree",
        issue_branch="master",
        issue_status="open",
        issue_created_at="2026-04-23 12:00:00",
    )

    _insert_flaky_issue_pr_link(
        sqlite_engine,
        issue_repo="pingcap/tidb",
        issue_number=71001,
        pr_repo="pingcap/tidb",
        pr_number=72001,
        linked_at="2026-04-06 10:00:00",
    )
    _insert_flaky_issue_pr_link(
        sqlite_engine,
        issue_repo="pingcap/tidb",
        issue_number=71002,
        pr_repo="pingcap/tidb",
        pr_number=72002,
        linked_at="2026-04-18 10:00:00",
    )
    _insert_flaky_issue_pr_link(
        sqlite_engine,
        issue_repo="pingcap/tidb",
        issue_number=71003,
        pr_repo="pingcap/tidb",
        pr_number=72003,
        linked_at="2026-04-24 10:00:00",
    )

    _insert_pull_ticket(
        sqlite_engine,
        repo="pingcap/tidb",
        number=72001,
        state="open",
        created_at="2026-04-06 10:30:00",
    )
    _insert_pull_ticket(
        sqlite_engine,
        repo="pingcap/tidb",
        number=72002,
        state="closed",
        created_at="2026-04-18 10:30:00",
        closed_at="2026-04-24 13:00:00",
        merged=1,
        merged_at="2026-04-24 13:00:00",
    )
    _insert_pull_ticket(
        sqlite_engine,
        repo="pingcap/tidb",
        number=72003,
        state="open",
        created_at="2026-04-24 10:30:00",
    )

    response = api_client.get(
        "/api/v1/pages/flaky",
        params={
            "repo": "pingcap/tidb",
            "branch": "master",
            "start_date": "2026-04-01",
            "end_date": "2026-04-29",
        },
    )

    assert response.status_code == 200
    progress = response.json()["issue_fix_progress"]
    assert progress["meta"]["as_of_date"] == "2026-04-29"
    assert progress["meta"]["comparison_as_of_date"] == "2026-04-22"
    assert progress["meta"]["ignores_start_date"] is True
    assert progress["meta"]["ignores_job_name"] is True
    assert progress["meta"]["ignores_cloud_phase"] is True
    assert progress["meta"]["ignores_issue_status"] is True
    assert progress["meta"]["ignores_branch_for_issues"] is True
    assert progress["meta"]["applies_branch_to_prs"] is False
    assert progress["filed_issue_count"] == 5
    assert progress["filed_issue_delta"] == 1
    assert progress["fixed_issue_count"] == 2
    assert progress["fixed_issue_delta"] == 1
    assert progress["in_review_pr_count"] == 2
    assert progress["in_review_pr_delta"] == 0
    assert progress["merged_pr_count"] == 1
    assert progress["merged_pr_delta"] == 1


def test_flaky_issue_queries_ignore_branch_filter(sqlite_engine) -> None:
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=72001,
        case_name="ReleaseScopedCase",
        issue_branch="release-8.5",
        issue_status="open",
        issue_created_at="2026-04-10 10:00:00",
    )
    _insert_flaky_issue(
        sqlite_engine,
        repo="pingcap/tidb",
        issue_number=72002,
        case_name="MasterScopedCase",
        issue_branch="master",
        issue_status="closed",
        issue_created_at="2026-04-11 10:00:00",
        issue_closed_at="2026-04-12 12:00:00",
    )
    _insert_flaky_issue_pr_link(
        sqlite_engine,
        issue_repo="pingcap/tidb",
        issue_number=72001,
        pr_repo="pingcap/tidb",
        pr_number=73001,
        linked_at="2026-04-10 11:00:00",
    )
    _insert_flaky_issue_pr_link(
        sqlite_engine,
        issue_repo="pingcap/tidb",
        issue_number=72002,
        pr_repo="pingcap/tidb",
        pr_number=73002,
        linked_at="2026-04-11 11:00:00",
    )
    _insert_pull_ticket(
        sqlite_engine,
        repo="pingcap/tidb",
        number=73001,
        state="open",
        created_at="2026-04-10 11:05:00",
    )
    _insert_pull_ticket(
        sqlite_engine,
        repo="pingcap/tidb",
        number=73002,
        state="closed",
        created_at="2026-04-11 11:05:00",
        closed_at="2026-04-12 12:00:00",
        merged=1,
        merged_at="2026-04-12 12:00:00",
    )

    app.dependency_overrides[get_engine] = lambda: sqlite_engine
    try:
        with TestClient(app) as client:
            page_response = client.get(
                "/api/v1/pages/flaky",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-20",
                },
            )
            issue_rates_response = client.get(
                "/api/v1/flaky/issue-weekly-rates",
                params={
                    "repo": "pingcap/tidb",
                    "branch": "master",
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-20",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert page_response.status_code == 200
    assert issue_rates_response.status_code == 200

    page_body = page_response.json()
    issue_fix_progress = page_body["issue_fix_progress"]
    assert issue_fix_progress["meta"]["ignores_branch_for_issues"] is True
    assert issue_fix_progress["meta"]["applies_branch_to_prs"] is False
    assert issue_fix_progress["filed_issue_count"] == 2
    assert issue_fix_progress["fixed_issue_count"] == 1
    assert issue_fix_progress["in_review_pr_count"] == 1
    assert issue_fix_progress["merged_pr_count"] == 1

    issue_lifecycle = page_body["issue_lifecycle"]
    assert issue_lifecycle["meta"]["ignores_branch"] is True
    assert issue_lifecycle["scoped_issue_count"] == 2

    issue_lifecycle_weekly = page_body["issue_lifecycle_weekly"]
    assert issue_lifecycle_weekly["meta"]["ignores_branch"] is True

    issue_rates_body = issue_rates_response.json()
    assert issue_rates_body["meta"]["ignores_branch"] is True
    rows_by_issue = {
        row["issue_number"]: row
        for row in issue_rates_body["rows"]
    }
    assert rows_by_issue[72001]["issue_branch"] == "release-8.5"
    assert rows_by_issue[72001]["display_name"] == "[release-8.5] ReleaseScopedCase"
    assert rows_by_issue[72002]["display_name"] == "[master] MasterScopedCase"
