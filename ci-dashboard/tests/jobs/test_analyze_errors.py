from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from ci_dashboard.common.config import (
    ArchiveSettings,
    DatabaseSettings,
    JobSettings,
    LLMSettings,
    Settings,
)
from ci_dashboard.common.models import ErrorClassification
from ci_dashboard.jobs.analyze_errors import (
    _append_live_signal_excerpts,
    _normalize_review_category,
    _parse_json_mapping,
    _should_refresh_existing_machine_classification,
    review_error_classification,
    run_analyze_errors,
)
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


class _FakeJenkinsFetcher:
    def __init__(self, excerpts_by_url: dict[str, str]) -> None:
        self.excerpts_by_url = excerpts_by_url
        self.calls: list[str] = []

    def fetch_console_signal_excerpts(self, build_url: str) -> str:
        self.calls.append(build_url)
        return self.excerpts_by_url.get(build_url, "")


def _insert_build(
    sqlite_engine,
    *,
    build_id: int,
    job_name: str,
    log_gcs_uri: str | None,
    pod_name: str | None = None,
    normalized_build_url: str | None = None,
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
                  context, url, normalized_build_url, author, pod_name, start_time, completion_time,
                  total_seconds, head_sha, target_branch, cloud_phase, build_system, log_gcs_uri,
                  error_l1_category, error_l2_subcategory, revise_error_l1_category, revise_error_l2_subcategory
                ) VALUES (
                  :id, NULL, :source_prow_job_id, NULL, :job_name, :job_type, :state,
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb', 'master', :pr_number, 1,
                  'unit',
                  :url,
                  :normalized_build_url,
                  'alice', :pod_name, :start_time, :completion_time,
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
                "normalized_build_url": normalized_build_url
                or f"https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/{job_name}/{build_id}/",
                "pod_name": pod_name,
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


def _insert_pod_lifecycle(
    sqlite_engine,
    *,
    source_prow_job_id: str | None = None,
    pod_name: str,
    normalized_build_url: str | None = None,
    abnormal_reason: str | None = None,
    abnormal_message: str | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pod_lifecycle (
                  source_project, cluster_name, location, namespace_name, pod_name, pod_uid,
                  build_system, source_prow_job_id, normalized_build_url, repo_full_name, job_name,
                  abnormal_reason, abnormal_message
                ) VALUES (
                  'prow', 'prow', 'us-central1-c', 'jenkins-tidb', :pod_name, :pod_uid,
                  'JENKINS', :source_prow_job_id, :normalized_build_url, 'pingcap/tidb', 'pingcap/tidb/ghpr_check2',
                  :abnormal_reason, :abnormal_message
                )
                """
            ),
            {
                "source_prow_job_id": source_prow_job_id,
                "pod_name": pod_name,
                "pod_uid": f"uid-{source_prow_job_id or pod_name}",
                "normalized_build_url": normalized_build_url
                or "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2602/",
                "abnormal_reason": abnormal_reason,
                "abnormal_message": abnormal_message,
            },
        )


def _insert_pod_event(
    sqlite_engine,
    *,
    pod_name: str,
    reporting_instance: str | None,
    event_reason: str,
    event_message: str,
    source_insert_id: str,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pod_events (
                  source_project, cluster_name, location, namespace_name, pod_name, pod_uid,
                  event_reason, event_type, event_message, event_timestamp, receive_timestamp,
                  reporting_component, reporting_instance, source_insert_id
                ) VALUES (
                  'prow', 'prow', 'us-central1-c', 'jenkins-tidb', :pod_name, :pod_uid,
                  :event_reason, 'Normal', :event_message, '2026-04-24 10:01:00', '2026-04-24 10:01:01',
                  'kubelet', :reporting_instance, :source_insert_id
                )
                """
            ),
            {
                "pod_name": pod_name,
                "pod_uid": f"uid-{pod_name}",
                "event_reason": event_reason,
                "event_message": event_message,
                "reporting_instance": reporting_instance,
                "source_insert_id": source_insert_id,
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


def test_run_analyze_errors_scans_partial_machine_classification(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=102,
        job_name="ghpr_check2",
        log_gcs_uri="gcs://ci-dashboard-test/2604/102.log",
        error_l1_category="OTHERS",
        error_l2_subcategory=None,
    )
    reader = _FakeReader(
        {
            ("ci-dashboard-test", "2604/102.log"): "fatal: dial tcp 10.0.0.1:443: i/o timeout",
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

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 102")
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


def test_run_analyze_errors_classifies_spot_preempted_from_pod_evidence(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=211,
        job_name="ghpr_check2",
        log_gcs_uri="gcs://ci-dashboard-test/2604/211.log",
        source_prow_job_id="prow-job-211",
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        source_prow_job_id="prow-job-211",
        pod_name="pingcap-tidb-ghpr-check2-211-abcd",
    )
    _insert_pod_event(
        sqlite_engine,
        pod_name="pingcap-tidb-ghpr-check2-211-abcd",
        reporting_instance="gke-prow-nap-c4d-highcpu-32-spot-1ops-91988024-qk46",
        event_reason="Killing",
        event_message="Stopping container jnlp",
        source_insert_id="event-211-killing",
    )

    reader = _FakeReader(
        {
            (
                "ci-dashboard-test",
                "2604/211.log",
            ): (
                "Pod [Failed][TerminationByKubelet] Pod was terminated in response to imminent node shutdown.\n"
                "Timeout waiting for agent to come back\n"
                "Finished: ABORTED\n"
            ),
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
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 211")
        ).mappings().one()

    assert row["error_l1_category"] == "INFRA"
    assert row["error_l2_subcategory"] == "SPOT_PREEMPTED"


def test_run_analyze_errors_loads_spot_evidence_via_normalized_build_url(sqlite_engine) -> None:
    normalized_build_url = "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/311/"
    pod_name = "pingcap-tidb-ghpr-check2-311-abcd"
    _insert_build(
        sqlite_engine,
        build_id=311,
        job_name="ghpr_check2",
        log_gcs_uri="gcs://ci-dashboard-test/2604/311.log",
        normalized_build_url=normalized_build_url,
        source_prow_job_id=None,
        pod_name=pod_name,
    )
    _insert_pod_lifecycle(
        sqlite_engine,
        source_prow_job_id=None,
        normalized_build_url=normalized_build_url,
        pod_name=pod_name,
    )
    _insert_pod_event(
        sqlite_engine,
        pod_name=pod_name,
        reporting_instance="gke-prow-nap-c4d-highcpu-32-spot-1ops-91988024-qk46",
        event_reason="Killing",
        event_message="Stopping container jnlp",
        source_insert_id="event-311-killing",
    )

    reader = _FakeReader(
        {
            (
                "ci-dashboard-test",
                "2604/311.log",
            ): (
                "Pod [Failed][TerminationByKubelet] Pod was terminated in response to imminent node shutdown.\n"
                "Timeout waiting for agent to come back\n"
                "Finished: ABORTED\n"
            ),
        }
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=reader,
        rule_engine=RuleEngine.from_file(),
        llm_classifier=_FakeClassifier(
            ErrorClassification(
                l1_category="OTHERS",
                l2_subcategory="UNCLASSIFIED",
                source="llm:test",
            )
        ),
    )

    assert summary.builds_scanned == 1
    assert summary.builds_classified == 1
    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 311")
        ).mappings().one()

    assert row["error_l1_category"] == "INFRA"
    assert row["error_l2_subcategory"] == "SPOT_PREEMPTED"


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


def test_run_analyze_errors_overrides_existing_groovy_with_trigger_plugin_superseded(
    sqlite_engine,
) -> None:
    _insert_build(
        sqlite_engine,
        build_id=171,
        source_prow_job_id="trigger-race-old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68180,
        head_sha="old-sha",
        start_time="2026-05-09 02:43:50",
        completion_time="2026-05-09 02:56:49",
        state="failure",
        log_gcs_uri="gcs://ci-dashboard-test/2605/171.log",
        error_l1_category="INFRA",
        error_l2_subcategory="JENKINS_GROOVY",
    )
    _insert_build(
        sqlite_engine,
        build_id=172,
        source_prow_job_id="trigger-race-new-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68180,
        head_sha="new-sha",
        start_time="2026-05-09 02:55:18",
        completion_time="2026-05-09 03:17:48",
        state="success",
        log_gcs_uri="gcs://ci-dashboard-test/2605/172.log",
    )
    _insert_prow_job(
        sqlite_engine,
        row_id=904,
        prow_job_id="trigger-race-old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        state="aborted",
        pr_number=68180,
        start_time="2026-05-09 02:43:50",
        description="Aborted by trigger plugin.",
    )
    reader = _FakeReader(
        {
            (
                "ci-dashboard-test",
                "2605/171.log",
            ): "groovy.lang.MissingPropertyException: No such property: WORKSPACE for class: groovy.lang.Binding\n",
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
    assert summary.builds_classified == 1
    assert summary.builds_rule_classified == 1
    assert reader.calls == []
    assert classifier.calls == []

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 171")
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


def test_run_analyze_errors_reclassifies_remoting_archive_with_live_oom_excerpt(sqlite_engine) -> None:
    build_id = 451
    job_name = "pull_cdc_storage_integration_light"
    build_url = f"https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/{job_name}/{build_id}/"
    _insert_build(
        sqlite_engine,
        build_id=build_id,
        job_name=job_name,
        log_gcs_uri="gcs://ci-dashboard-test/2605/451.log",
        error_l1_category="INFRA",
        error_l2_subcategory="JENKINS",
    )
    reader = _FakeReader(
        {
            (
                "ci-dashboard-test",
                "2605/451.log",
            ): (
                "hudson.remoting.ChannelClosedException\n"
                "removed or offline for 5 min\n"
                "Timeout waiting for agent to come back\n"
            ),
        }
    )
    jenkins_fetcher = _FakeJenkinsFetcher(
        {
            build_url: (
                "===== Jenkins console signal excerpts =====\n"
                "----- lines 1880-1884 -----\n"
                "Container [golang] terminated [OOMKilled]\n"
            )
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
        force=True,
        reader=reader,
        jenkins_fetcher=jenkins_fetcher,
        rule_engine=RuleEngine.from_file(),
        llm_classifier=classifier,
    )

    assert summary.builds_scanned == 1
    assert summary.builds_rule_classified == 1
    assert summary.builds_llm_classified == 0
    assert reader.calls == [("ci-dashboard-test", "2605/451.log")]
    assert jenkins_fetcher.calls == [build_url]
    assert classifier.calls == []

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT error_l1_category, error_l2_subcategory FROM ci_l1_builds WHERE id = 451")
        ).mappings().one()

    assert row["error_l1_category"] == "INFRA"
    assert row["error_l2_subcategory"] == "OOMKILLED"


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


def test_run_analyze_errors_skips_existing_machine_classification_without_force(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=601,
        source_prow_job_id="skip-old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68188,
        head_sha="same-sha",
        start_time="2026-05-06 09:00:00",
        completion_time="2026-05-06 09:05:00",
        state="failure",
        log_gcs_uri=None,
        error_l1_category="INFRA",
        error_l2_subcategory="NETWORK",
    )
    _insert_build(
        sqlite_engine,
        build_id=602,
        source_prow_job_id="skip-new-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        job_type="presubmit",
        pr_number=68188,
        head_sha="same-sha",
        start_time="2026-05-06 09:10:00",
        completion_time="2026-05-06 09:20:00",
        state="success",
        log_gcs_uri="gcs://ci-dashboard-test/2604/602.log",
    )
    _insert_prow_job(
        sqlite_engine,
        row_id=903,
        prow_job_id="skip-old-prow-job",
        job_name="pingcap/tidb/ghpr_check2",
        state="aborted",
        pr_number=68188,
        start_time="2026-05-06 09:00:00",
        description="Aborted by admin.",
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=_FakeReader({}),
        rule_engine=RuleEngine.from_file(),
    )

    assert summary.builds_scanned == 1
    assert summary.builds_skipped == 1
    assert summary.builds_classified == 0


def test_run_analyze_errors_counts_reader_failures(sqlite_engine) -> None:
    class _BoomReader:
        def download_text(self, *, bucket: str, object_name: str, encoding: str = "utf-8") -> str:
            del bucket, object_name, encoding
            raise RuntimeError("boom")

    _insert_build(
        sqlite_engine,
        build_id=602,
        job_name="ghpr_check2",
        log_gcs_uri="gcs://ci-dashboard-test/2604/602.log",
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=_BoomReader(),
        rule_engine=RuleEngine.from_file(),
    )

    assert summary.builds_scanned == 1
    assert summary.builds_failed == 1
    assert summary.builds_classified == 0


def test_run_analyze_errors_skips_invalid_log_uri(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=603,
        job_name="ghpr_check2",
        log_gcs_uri="not-a-gcs-uri",
    )

    summary = run_analyze_errors(
        sqlite_engine,
        _settings(),
        reader=_FakeReader({}),
        rule_engine=RuleEngine.from_file(),
        llm_classifier=_FakeClassifier(
            ErrorClassification(
                l1_category="OTHERS",
                l2_subcategory="UNCLASSIFIED",
                source="llm:test",
            )
        ),
    )

    assert summary.builds_scanned == 1
    assert summary.builds_skipped == 1
    assert summary.builds_classified == 0


def test_parse_json_mapping_handles_bytes_invalid_json_and_non_mapping() -> None:
    assert _parse_json_mapping(b'{\"description\": \"hello\"}') == {"description": "hello"}
    assert _parse_json_mapping("{not-json") == {}
    assert _parse_json_mapping("[1, 2, 3]") == {}


def test_should_refresh_existing_machine_classification_handles_abort_paths() -> None:
    assert _should_refresh_existing_machine_classification({"error_l1_category": "", "error_l2_subcategory": ""}) is True
    assert (
        _should_refresh_existing_machine_classification(
            {
                "error_l1_category": "OTHERS",
                "error_l2_subcategory": "SUPERSEDED_BY_NEWER_BUILD",
            }
        )
        is False
    )
    assert (
        _should_refresh_existing_machine_classification(
            {
                "error_l1_category": "INFRA",
                "error_l2_subcategory": "NETWORK",
                "prow_state": "failure",
            }
        )
        is False
    )
    assert (
        _should_refresh_existing_machine_classification(
            {
                "error_l1_category": "INFRA",
                "error_l2_subcategory": "NETWORK",
                "prow_state": "aborted",
                "has_newer_pr_job_version_with_different_sha": "true",
            }
        )
        is True
    )
    assert (
        _should_refresh_existing_machine_classification(
            {
                "error_l1_category": "INFRA",
                "error_l2_subcategory": "NETWORK",
                "prow_state": "aborted",
                "prow_status_description": "Aborted as the newer version of this job is running.",
                "has_newer_pr_job_version": "1",
            }
        )
        is True
    )


def test_normalize_review_category_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _normalize_review_category("   ")


def test_append_live_signal_excerpts_handles_missing_fetcher_url_and_signal() -> None:
    assert (
        _append_live_signal_excerpts(
            "tail",
            build={"url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/1/"},
            jenkins_fetcher=object(),
        )
        == "tail"
    )
    fetcher = _FakeJenkinsFetcher({})
    assert _append_live_signal_excerpts("tail", build={"url": ""}, jenkins_fetcher=fetcher) == "tail"
    assert (
        _append_live_signal_excerpts(
            "tail",
            build={"url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/1/"},
            jenkins_fetcher=fetcher,
        )
        == "tail"
    )
