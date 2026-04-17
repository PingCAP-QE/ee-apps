from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from ci_dashboard.common.db import install_sqlite_functions


def _create_test_schema(engine: Engine) -> None:
    statements = [
        """
        CREATE TABLE prow_jobs (
          id INTEGER PRIMARY KEY,
          prowJobId TEXT NOT NULL,
          namespace TEXT NOT NULL,
          jobName TEXT NOT NULL,
          type TEXT NOT NULL,
          state TEXT NOT NULL,
          optional INTEGER,
          report INTEGER,
          org TEXT NOT NULL,
          repo TEXT NOT NULL,
          base_ref TEXT,
          pull INTEGER,
          context TEXT,
          url TEXT NOT NULL,
          author TEXT,
          retest INTEGER,
          event_guid TEXT,
          startTime TEXT,
          completionTime TEXT,
          spec TEXT,
          status TEXT
        )
        """,
        """
        CREATE TABLE ci_job_state (
          job_name TEXT PRIMARY KEY,
          watermark_json TEXT NOT NULL,
          last_started_at TEXT NULL,
          last_succeeded_at TEXT NULL,
          last_status TEXT NOT NULL DEFAULT 'never',
          last_error TEXT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE ci_l1_builds (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_prow_row_id INTEGER NOT NULL,
          source_prow_job_id TEXT NOT NULL UNIQUE,
          namespace TEXT NOT NULL,
          job_name TEXT NOT NULL,
          job_type TEXT NOT NULL,
          state TEXT NOT NULL,
          optional INTEGER NOT NULL DEFAULT 0,
          report INTEGER NOT NULL DEFAULT 0,
          org TEXT NOT NULL,
          repo TEXT NOT NULL,
          repo_full_name TEXT NOT NULL,
          base_ref TEXT NULL,
          pr_number INTEGER NULL,
          is_pr_build INTEGER NOT NULL DEFAULT 0,
          context TEXT NULL,
          url TEXT NOT NULL,
          normalized_build_key TEXT NULL,
          author TEXT NULL,
          retest INTEGER NULL,
          event_guid TEXT NULL,
          build_id TEXT NULL,
          pod_name TEXT NULL,
          pending_time TEXT NULL,
          start_time TEXT NOT NULL,
          completion_time TEXT NULL,
          queue_wait_seconds INTEGER NULL,
          run_seconds INTEGER NULL,
          total_seconds INTEGER NULL,
          head_sha TEXT NULL,
          target_branch TEXT NULL,
          cloud_phase TEXT NOT NULL DEFAULT 'IDC',
          is_flaky INTEGER NOT NULL DEFAULT 0,
          is_retry_loop INTEGER NOT NULL DEFAULT 0,
          has_flaky_case_match INTEGER NOT NULL DEFAULT 0,
          failure_category TEXT NULL,
          failure_subcategory TEXT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE ci_l1_pr_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repo TEXT NOT NULL,
          pr_number INTEGER NOT NULL,
          event_key TEXT NOT NULL,
          event_time TEXT NOT NULL,
          event_type TEXT NOT NULL,
          actor_login TEXT NULL,
          comment_id INTEGER NULL,
          comment_body TEXT NULL,
          retest_event INTEGER NOT NULL DEFAULT 0,
          commit_sha TEXT NULL,
          target_branch TEXT NULL,
          head_ref TEXT NULL,
          head_sha TEXT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(repo, pr_number, event_key)
        )
        """,
        """
        CREATE TABLE github_tickets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          type TEXT NOT NULL,
          repo TEXT NOT NULL,
          number INTEGER NOT NULL,
          title TEXT NULL,
          body TEXT NULL,
          state TEXT NULL,
          created_at TEXT NULL,
          updated_at TEXT NULL,
          timeline TEXT NULL,
          branches TEXT NULL
        )
        """,
        """
        CREATE TABLE problem_case_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repo TEXT NOT NULL,
          branch TEXT NULL,
          suite_name TEXT NULL,
          case_name TEXT NULL,
          flaky INTEGER NOT NULL DEFAULT 0,
          timecost_ms INTEGER NULL,
          report_time TEXT NULL,
          build_url TEXT NULL,
          reason TEXT NULL
        )
        """,
        """
        CREATE TABLE ci_l1_flaky_issues (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          repo TEXT NOT NULL,
          issue_number INTEGER NOT NULL,
          issue_url TEXT NOT NULL,
          issue_title TEXT NOT NULL,
          case_name TEXT NOT NULL,
          issue_status TEXT NOT NULL,
          issue_branch TEXT NULL,
          branch_source TEXT NOT NULL DEFAULT 'unknown',
          issue_created_at TEXT NOT NULL,
          issue_updated_at TEXT NOT NULL,
          issue_closed_at TEXT NULL,
          last_reopened_at TEXT NULL,
          reopen_count INTEGER NOT NULL DEFAULT 0,
          source_ticket_id INTEGER NOT NULL,
          source_ticket_updated_at TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(repo, issue_number)
        )
        """,
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


@pytest.fixture()
def sqlite_engine(tmp_path: Path) -> Engine:
    db_path = tmp_path / "ci-dashboard-test.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    install_sqlite_functions(engine)
    _create_test_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()
