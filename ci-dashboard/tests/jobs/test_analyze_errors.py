from __future__ import annotations

import json

from sqlalchemy import text

from ci_dashboard.common.config import (
    ArchiveSettings,
    DatabaseSettings,
    JobSettings,
    LLMSettings,
    Settings,
)
from ci_dashboard.common.models import ErrorClassification
from ci_dashboard.jobs.analyze_errors import review_error_classification, run_analyze_errors
from ci_dashboard.jobs.rule_engine import RuleEngine


def _settings() -> Settings:
    return Settings(
        database=DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=None,
        ),
        jobs=JobSettings(batch_size=10),
        archive=ArchiveSettings(gcs_bucket="ci-dashboard-test"),
        llm=LLMSettings(provider="noop"),
        log_level="INFO",
    )


class _FakeReader:
    def __init__(self, objects: dict[tuple[str, str], str]) -> None:
        self.objects = objects
        self.calls: list[tuple[str, str]] = []

    def download_text(self, *, bucket: str, object_name: str, encoding: str = "utf-8") -> str:
        del encoding
        self.calls.append((bucket, object_name))
        return self.objects[(bucket, object_name)]


class _FakeClassifier:
    def __init__(self, classification: ErrorClassification) -> None:
        self.classification = classification
        self.calls: list[int] = []

    def classify(self, *, log_text: str, build) -> ErrorClassification:
        del log_text
        self.calls.append(int(build["id"]))
        return self.classification


def _insert_build(
    sqlite_engine,
    *,
    build_id: int,
    job_name: str,
    log_gcs_uri: str | None,
    source_prow_job_id: str | None = None,
    job_type: str | None = None,
    pr_number: int | None = None,
    head_sha: str | None = None,
    start_time: str = "2026-04-24 10:00:00",
    completion_time: str = "2026-04-24 10:20:00",
    state: str = "failure",
    error_l1_category: str | None = None,
    error_l2_subcategory: str | None = None,
    revise_error_l1_category: str | None = None,
    revise_error_l2_subcategory: str | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  id, source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, base_ref, pr_number, is_pr_build,
                  context, url, normalized_build_url, author, start_time, completion_time,
                  total_seconds, head_sha, target_branch, cloud_phase, build_system, log_gcs_uri,
                  error_l1_category, error_l2_subcategory, revise_error_l1_category, revise_error_l2_subcategory
                ) VALUES (
                  :id, NULL, :source_prow_job_id, NULL, :job_name, :job_type, :state,
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb', 'master', :pr_number, 1,
                  'unit',
                  :url,
                  :normalized_build_url,
                  'alice', :start_time, :completion_time,
                  1200, :head_sha, 'master', 'GCP', 'JENKINS', :log_gcs_uri,
                  :error_l1_category, :error_l2_subcategory, :revise_error_l1_category, :revise_error_l2_subcategory
                )
                """
            ),
            {
                "id": build_id,
                "source_prow_job_id": source_prow_job_id,
                "job_name": job_name,
                "job_type": job_type,
                "state": state,
                "pr_number": build_id if pr_number is None else pr_number,
                "url": f"https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/{job_name}/{build_id}/",
                "normalized_build_url": (
                    f"https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/{job_name}/{build_id}/"
                ),
                "start_time": start_time,
                "completion_time": completion_time,
                "head_sha": head_sha,
                "log_gcs_uri": log_gcs_uri,
                "error_l1_category": error_l1_category,
                "error_l2_subcategory": error_l2_subcategory,
                "revise_error_l1_category": revise_error_l1_category,
                "revise_error_l2_subcategory": revise_error_l2_subcategory,
            },
        )


def _insert_prow_job(
    sqlite_engine,
    *,
    row_id: int,
    prow_job_id: str,
    job_name: str,
    state: str,
    pr_number: int,
    start_time: str,
    description: str,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO prow_jobs (
                  id, prowJobId, namespace, jobName, type, state,
                  optional, report, org, repo, base_ref, pull, context, url,
                  author, retest, event_guid, startTime, completionTime, spec, status
                ) VALUES (
                  :id, :prow_job_id, 'prow', :job_name, 'presubmit', :state,
                  0, 1, 'pingcap', 'tidb', 'master', :pr_number, 'check_dev_2', :url,
                  'alice', 0, :event_guid, :start_time, :completion_time, :spec, :status
                )
                """
            ),
            {
                "id": row_id,
                "prow_job_id": prow_job_id,
                "job_name": job_name,
                "state": state,
                "pr_number": pr_number,
                "url": f"https://prow.tidb.net/jenkins/job/{job_name}/{row_id}/display/redirect",
                "event_guid": f"event-{row_id}",
                "start_time": start_time,
                "completion_time": start_time,
                "spec": json.dumps({"refs": {"pulls": [{"number": pr_number, "sha": f"sha-{row_id}"}]}}),
                "status": json.dumps({"description": description}),
            },
        )


def test_run_analyze_errors_classifies_via_rules(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=101,
        job_name="ghpr_check2",
        log_gcs_uri="gcs://ci-dashboard-test/2604/101.log",
    )
    reader = _FakeReader(
        {
            ("ci-dashboard-test", "2604/101.log"): "fatal: dial tcp 10.0.0.1:443: i/o timeout",
        }
    )
    classifier = _FakeClassifier(
        ErrorClassification(
            l1_category="OTHERS",
            l2_subcategory="UNCLASSIFIED",
            source="llm:test",
        )
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=reader,
        rule_engine=RuleEngine.from_file(),
        llm_classifier=classifier,
    )

    assert summary.builds_scanned == 1
    assert summary.builds_classified == 1
    assert summary.builds_rule_classified == 1
    assert summary.builds_llm_classified == 0
    assert classifier.calls == []

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 101")
        ).mappings().one()

    assert row["error_l1_category"] == "INFRA"
    assert row["error_l2_subcategory"] == "NETWORK"


def test_run_analyze_errors_classifies_prow_superseded_abort_from_metadata(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=121,
        source_prow_job_id="old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68140,
        head_sha="old-sha",
        start_time="2026-04-30 10:01:45",
        completion_time="2026-04-30 10:06:33",
        state="failure",
        log_gcs_uri=None,
    )
    _insert_build(
        sqlite_engine,
        build_id=122,
        source_prow_job_id="new-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68140,
        head_sha="new-sha",
        start_time="2026-04-30 10:06:02",
        completion_time="2026-04-30 10:20:32",
        state="success",
        log_gcs_uri="gcs://ci-dashboard-test/2604/122.log",
    )
    _insert_prow_job(
        sqlite_engine,
        row_id=901,
        prow_job_id="old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        state="aborted",
        pr_number=68140,
        start_time="2026-04-30 10:01:45",
        description="Aborted as the newer version of this job is running.",
    )
    reader = _FakeReader({})
    classifier = _FakeClassifier(
        ErrorClassification(
            l1_category="BUILD",
            l2_subcategory="PIPELINE_CONFIG",
            source="llm:test",
        )
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=reader,
        rule_engine=RuleEngine.from_file(),
        llm_classifier=classifier,
    )

    assert summary.builds_scanned == 1
    assert summary.builds_rule_classified == 1
    assert reader.calls == []
    assert classifier.calls == []

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 121")
        ).mappings().one()

    assert row["error_l1_category"] == "OTHERS"
    assert row["error_l2_subcategory"] == "SUPERSEDED_BY_NEWER_BUILD"


def test_run_analyze_errors_classifies_trigger_plugin_abort_with_newer_sha(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=131,
        source_prow_job_id="trigger-old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68157,
        head_sha="old-sha",
        start_time="2026-05-04 09:36:12",
        completion_time="2026-05-04 09:42:37",
        state="failure",
        log_gcs_uri=None,
    )
    _insert_build(
        sqlite_engine,
        build_id=132,
        source_prow_job_id="trigger-new-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68157,
        head_sha="new-sha",
        start_time="2026-05-04 09:44:38",
        completion_time="2026-05-04 10:02:37",
        state="success",
        log_gcs_uri="gcs://ci-dashboard-test/2605/132.log",
    )
    _insert_prow_job(
        sqlite_engine,
        row_id=902,
        prow_job_id="trigger-old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        state="aborted",
        pr_number=68157,
        start_time="2026-05-04 09:36:12",
        description="Aborted by trigger plugin.",
    )
    reader = _FakeReader({})
    classifier = _FakeClassifier(
        ErrorClassification(
            l1_category="BUILD",
            l2_subcategory="PIPELINE_CONFIG",
            source="llm:test",
        )
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=reader,
        rule_engine=RuleEngine.from_file(),
        llm_classifier=classifier,
    )

    assert summary.builds_scanned == 1
    assert summary.builds_rule_classified == 1
    assert reader.calls == []
    assert classifier.calls == []

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 131")
        ).mappings().one()

    assert row["error_l1_category"] == "OTHERS"
    assert row["error_l2_subcategory"] == "SUPERSEDED_BY_NEWER_BUILD"


def test_run_analyze_errors_classifies_generic_admin_abort_with_newer_sha_after_log(
    sqlite_engine,
) -> None:
    _insert_build(
        sqlite_engine,
        build_id=141,
        source_prow_job_id="generic-old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68146,
        head_sha="old-sha",
        start_time="2026-05-03 16:06:19",
        completion_time="2026-05-03 16:08:30",
        state="failure",
        log_gcs_uri="gcs://ci-dashboard-test/2605/141.log",
    )
    _insert_build(
        sqlite_engine,
        build_id=142,
        source_prow_job_id="generic-new-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68146,
        head_sha="new-sha",
        start_time="2026-05-03 16:18:30",
        completion_time="2026-05-03 16:32:00",
        state="success",
        log_gcs_uri="gcs://ci-dashboard-test/2605/142.log",
    )
    _insert_prow_job(
        sqlite_engine,
        row_id=903,
        prow_job_id="generic-old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        state="aborted",
        pr_number=68146,
        start_time="2026-05-03 16:06:19",
        description="Jenkins job aborted.",
    )
    reader = _FakeReader(
        {
            (
                "ci-dashboard-test",
                "2605/141.log",
            ): "Aborted by Flare Zuo\nSending interrupt signal to process\nFinished: ABORTED\n",
        }
    )
    classifier = _FakeClassifier(
        ErrorClassification(
            l1_category="BUILD",
            l2_subcategory="PIPELINE_CONFIG",
            source="llm:test",
        )
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=reader,
        rule_engine=RuleEngine.from_file(),
        llm_classifier=classifier,
    )

    assert summary.builds_scanned == 1
    assert summary.builds_rule_classified == 1
    assert reader.calls == [("ci-dashboard-test", "2605/141.log")]
    assert classifier.calls == []

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 141")
        ).mappings().one()

    assert row["error_l1_category"] == "OTHERS"
    assert row["error_l2_subcategory"] == "SUPERSEDED_BY_NEWER_BUILD"


def test_run_analyze_errors_skips_success_rows_even_with_archived_log(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=151,
        job_name="ghpr_check2",
        state="success",
        log_gcs_uri="gcs://ci-dashboard-test/2604/151.log",
    )
    reader = _FakeReader(
        {
            ("ci-dashboard-test", "2604/151.log"): "fatal: dial tcp 10.0.0.1:443: i/o timeout",
        }
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        force=True,
        reader=reader,
        rule_engine=RuleEngine.from_file(),
    )

    assert summary.builds_scanned == 0
    assert reader.calls == []

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 151")
        ).mappings().one()

    assert row["error_l1_category"] is None
    assert row["error_l2_subcategory"] is None


def test_run_analyze_errors_falls_back_to_llm_classifier(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=201,
        job_name="mystery_job",
        log_gcs_uri="gcs://ci-dashboard-test/2604/201.log",
    )
    reader = _FakeReader(
        {
            ("ci-dashboard-test", "2604/201.log"): "some new unknown failure shape",
        }
    )
    classifier = _FakeClassifier(
        ErrorClassification(
            l1_category="OTHERS",
            l2_subcategory="UNCLASSIFIED",
            source="llm:test",
        )
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=reader,
        rule_engine=RuleEngine.from_file(),
        llm_classifier=classifier,
    )

    assert summary.builds_scanned == 1
    assert summary.builds_classified == 1
    assert summary.builds_rule_classified == 0
    assert summary.builds_llm_classified == 1
    assert classifier.calls == [201]


def test_run_analyze_errors_skips_reviewed_rows(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=301,
        job_name="ghpr_check2",
        log_gcs_uri="gcs://ci-dashboard-test/2604/301.log",
        revise_error_l1_category="INFRA",
        revise_error_l2_subcategory="NETWORK",
    )
    reader = _FakeReader({})

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=reader,
        rule_engine=RuleEngine.from_file(),
    )

    assert summary.builds_scanned == 0
    assert reader.calls == []


def test_run_analyze_errors_force_overwrites_machine_classification(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=401,
        job_name="ghpr_unit_test",
        log_gcs_uri="gcs://ci-dashboard-test/2604/401.log",
        error_l1_category="OTHERS",
        error_l2_subcategory="UNCLASSIFIED",
    )
    reader = _FakeReader(
        {
            ("ci-dashboard-test", "2604/401.log"): "--- FAIL: TestDDLBasic (0.00s)\nFAIL\n",
        }
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        force=True,
        reader=reader,
        rule_engine=RuleEngine.from_file(),
    )

    assert summary.builds_scanned == 1
    assert summary.builds_rule_classified == 1

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 401")
        ).mappings().one()

    assert row["error_l1_category"] == "UT"
    assert row["error_l2_subcategory"] == "TEST_FAILURE"


def test_review_error_classification_updates_revise_fields(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=501,
        job_name="ghpr_check2",
        log_gcs_uri="gcs://ci-dashboard-test/2604/501.log",
    )

    summary = review_error_classification(
        sqlite_engine,
        build_id=501,
        l1_category="infra",
        l2_subcategory="network",
    )

    assert summary.rows_updated == 1

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT revise_error_l1_category, revise_error_l2_subcategory FROM ci_l1_builds WHERE id = 501"
            )
        ).mappings().one()

    assert row["revise_error_l1_category"] == "INFRA"
    assert row["revise_error_l2_subcategory"] == "NETWORK"
