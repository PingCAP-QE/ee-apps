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
          source_prow_row_id INTEGER NULL,
          source_prow_job_id TEXT NULL UNIQUE,
          namespace TEXT NULL,
          job_name TEXT NULL,
          job_type TEXT NULL,
          state TEXT NOT NULL,
          optional INTEGER NOT NULL DEFAULT 0,
          report INTEGER NOT NULL DEFAULT 0,
          org TEXT NULL,
          repo TEXT NULL,
          repo_full_name TEXT NULL,
          base_ref TEXT NULL,
          pr_number INTEGER NULL,
          is_pr_build INTEGER NOT NULL DEFAULT 0,
          context TEXT NULL,
          url TEXT NOT NULL,
          normalized_build_url TEXT NULL,
          author TEXT NULL,
          retest INTEGER NULL,
          event_guid TEXT NULL,
          build_id TEXT NULL,
          pod_name TEXT NULL,
          pending_time TEXT NULL,
          start_time TEXT NULL,
          completion_time TEXT NULL,
          queue_wait_seconds INTEGER NULL,
          run_seconds INTEGER NULL,
          total_seconds INTEGER NULL,
          head_sha TEXT NULL,
          target_branch TEXT NULL,
          cloud_phase TEXT NOT NULL DEFAULT 'IDC',
          build_system TEXT NOT NULL DEFAULT 'UNKNOWN',
          source_jenkins_event_id TEXT NULL,
          source_jenkins_job_url TEXT NULL,
          source_jenkins_result TEXT NULL,
          build_params_json TEXT NULL,
          log_gcs_uri TEXT NULL,
          log_archived_at TEXT NULL,
          ai_error_l1_category TEXT NULL,
          ai_error_l2_subcategory TEXT NULL,
          ai_classification_source TEXT NULL,
          ai_classification_confidence REAL NULL,
          ai_classified_at TEXT NULL,
          ai_provider_name TEXT NULL,
          ai_model_name TEXT NULL,
          ai_evidence_text TEXT NULL,
          human_error_l1_category TEXT NULL,
          human_error_l2_subcategory TEXT NULL,
          human_reviewed_at TEXT NULL,
          human_reviewer TEXT NULL,
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
        CREATE TABLE ci_l1_jenkins_build_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL,
          event_time TEXT NULL,
          received_at TEXT NOT NULL,
          normalized_build_url TEXT NULL,
          build_url TEXT NULL,
          result TEXT NULL,
          payload_json TEXT NOT NULL,
          processing_status TEXT NOT NULL DEFAULT 'RECEIVED',
          last_error TEXT NULL,
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
          comments TEXT NULL,
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
          normalized_build_key TEXT NULL,
          cloud_phase TEXT NULL,
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
        """
        CREATE TABLE ci_l1_pod_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_project TEXT NOT NULL,
          cluster_name TEXT NULL,
          location TEXT NULL,
          namespace_name TEXT NULL,
          pod_name TEXT NULL,
          pod_uid TEXT NULL,
          event_reason TEXT NULL,
          event_type TEXT NULL,
          event_message TEXT NULL,
          event_timestamp TEXT NOT NULL,
          receive_timestamp TEXT NOT NULL,
          first_timestamp TEXT NULL,
          last_timestamp TEXT NULL,
          reporting_component TEXT NULL,
          reporting_instance TEXT NULL,
          source_insert_id TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(source_project, source_insert_id)
        )
        """,
        """
        CREATE TABLE ci_l1_pod_lifecycle (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_project TEXT NOT NULL,
          cluster_name TEXT NULL,
          location TEXT NULL,
          namespace_name TEXT NULL,
          pod_name TEXT NULL,
          pod_uid TEXT NULL,
          build_system TEXT NOT NULL DEFAULT 'UNKNOWN',
          pod_labels_json TEXT NULL,
          pod_annotations_json TEXT NULL,
          metadata_observed_at TEXT NULL,
          pod_author TEXT NULL,
          pod_org TEXT NULL,
          pod_repo TEXT NULL,
          jenkins_label TEXT NULL,
          jenkins_label_digest TEXT NULL,
          jenkins_controller TEXT NULL,
          ci_job TEXT NULL,
          source_prow_job_id TEXT NULL,
          normalized_build_url TEXT NULL,
          repo_full_name TEXT NULL,
          job_name TEXT NULL,
          scheduled_at TEXT NULL,
          first_pulling_at TEXT NULL,
          first_pulled_at TEXT NULL,
          first_created_at TEXT NULL,
          first_started_at TEXT NULL,
          last_failed_scheduling_at TEXT NULL,
          failed_scheduling_count INTEGER NOT NULL DEFAULT 0,
          last_event_at TEXT NULL,
          schedule_to_started_seconds INTEGER NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(source_project, namespace_name, pod_uid, pod_name)
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
