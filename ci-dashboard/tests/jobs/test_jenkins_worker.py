from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from urllib.parse import urlsplit

import pytest
from pymysql.constants import CLIENT
from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, KafkaSettings, Settings
from ci_dashboard.common.db import build_engine
from ci_dashboard.jobs import jenkins_worker as worker
from ci_dashboard.jobs.jenkins_worker import (
    _build_kafka_consumer,
    map_jenkins_result_to_state,
    parse_jenkins_finished_event,
    process_jenkins_event_message,
    run_consume_jenkins_events,
)


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
        log_level="INFO",
    )


class _NoopTimingsEnricher:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def submit(self, *, build_id: int, build_url: str) -> bool:
        self.calls.append((build_id, build_url))
        return True


def _finished_event_payload(*, event_id: str = "evt-1") -> dict[str, object]:
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "https://jenkins.example.internal/jenkins",
        "type": "dev.cdevents.pipelinerun.finished.0.1.0",
        "time": "2026-04-24T10:20:00Z",
        "subject": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/",
        "data": {
            "result": "FAILURE",
            "startTime": "2026-04-24T10:00:00Z",
            "completionTime": "2026-04-24T10:20:00Z",
            "customData": {
                "org": "pingcap",
                "repo": "tidb",
                "branch": "master",
                "pr": "301",
                "commit": "0123456789abcdef0123456789abcdef01234567",
                "author": "alice",
                "unsafe_token": "should-not-be-stored",
            },
        },
    }


def _real_jenkins_plugin_finished_event_payload(*, event_id: str = "evt-real-1") -> dict[str, object]:
    return {
        "specversion": "0.3",
        "id": event_id,
        "source": "job/pingcap/job/tidb/job/pull_mysql_client_test/1816/",
        "type": "dev.cdevents.pipelinerun.finished.0.1.0",
        "datacontenttype": "application/json",
        "time": "2026-04-27T13:09:00.499612608Z",
        "data": {
            "context": {
                "id": "ff67a5de-f3b1-403e-9c0a-d41b2103a108",
                "type": "dev.cdevents.pipelinerun.finished.0.1.0",
                "source": "job/pingcap/job/tidb/job/pull_mysql_client_test/1816/",
                "version": "0.1.2",
                "timestamp": "2026-04-27T13:09:00Z",
            },
            "customData": {
                "name": "pull_mysql_client_test",
                "displayName": "pull_mysql_client_test",
                "url": "job/pingcap/job/tidb/job/pull_mysql_client_test/",
                "build": {
                    "number": 1816,
                    "queueId": 202018,
                    "duration": 566556,
                    "url": "job/pingcap/job/tidb/job/pull_mysql_client_test/1816/",
                    "parameters": {
                        "BUILD_ID": "2048748867029045251",
                        "JOB_SPEC": json.dumps(
                            {
                                "type": "presubmit",
                                "job": "pingcap/tidb/pull_mysql_client_test",
                                "buildid": "2048748867029045251",
                                "prowjobid": "3bfe389c-e361-4a6d-a1d6-679fb87b0e13",
                                "refs": {
                                    "org": "pingcap",
                                    "repo": "tidb",
                                    "base_ref": "master",
                                    "pulls": [
                                        {
                                            "number": 65626,
                                            "author": "terry1purcell",
                                            "sha": "e6030436c2093b30da167ca295887b8df8eaeb07",
                                        }
                                    ],
                                },
                            }
                        ),
                        "PROW_JOB_ID": "3bfe389c-e361-4a6d-a1d6-679fb87b0e13",
                    },
                },
            },
            "customDataContentType": "application/json",
            "subject": {
                "id": "1816",
                "type": "PIPELINERUN",
                "content": {
                    "pipelineName": "pingcap » tidb » pull_mysql_client_test",
                    "outcome": "SUCCESS",
                    "errors": "",
                },
            },
        },
    }


def test_parse_jenkins_finished_event_extracts_canonical_fields() -> None:
    parsed = parse_jenkins_finished_event(_finished_event_payload(), _settings())

    assert parsed.event_id == "evt-1"
    assert parsed.normalized_build_url == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    assert parsed.build_url == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    assert parsed.state == "failure"
    assert parsed.jenkins_result == "FAILURE"
    assert parsed.job_name == "pingcap/tidb/ghpr_unit_test"
    assert parsed.repo_full_name == "pingcap/tidb"
    assert parsed.pr_number == 301
    assert parsed.is_pr_build is True
    assert parsed.target_branch == "master"
    assert parsed.total_seconds == 1200
    assert parsed.start_time == datetime(2026, 4, 24, 10, 0, 0)
    assert parsed.completion_time == datetime(2026, 4, 24, 10, 20, 0)


def test_parse_jenkins_finished_event_classifies_tencent_staging_url() -> None:
    payload = _finished_event_payload()
    payload["subject"] = (
        "https://do.pingcap.net/jenkins-staging/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    )

    parsed = parse_jenkins_finished_event(payload, _settings())

    assert parsed.normalized_build_url == (
        "https://do.pingcap.net/jenkins-staging/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    )
    assert parsed.cloud_phase == "TENCENT"
    assert parsed.build_system == "JENKINS"


def test_parse_jenkins_finished_event_supports_real_plugin_payload() -> None:
    parsed = parse_jenkins_finished_event(_real_jenkins_plugin_finished_event_payload(), _settings())

    assert parsed.build_url == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/"
    assert parsed.normalized_build_url == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/"
    assert parsed.source_prow_job_id == "3bfe389c-e361-4a6d-a1d6-679fb87b0e13"
    assert parsed.jenkins_result == "SUCCESS"
    assert parsed.state == "success"
    assert parsed.job_name == "pingcap/tidb/pull_mysql_client_test"
    assert parsed.job_type == "presubmit"
    assert parsed.org == "pingcap"
    assert parsed.repo == "tidb"
    assert parsed.repo_full_name == "pingcap/tidb"
    assert parsed.base_ref == "master"
    assert parsed.pr_number == 65626
    assert parsed.is_pr_build is True
    assert parsed.author == "terry1purcell"
    assert parsed.head_sha == "e6030436c2093b30da167ca295887b8df8eaeb07"
    assert parsed.build_id == "2048748867029045251"
    assert parsed.build_system == "JENKINS"
    assert parsed.cloud_phase == "GCP"
    assert parsed.run_seconds == 566
    assert parsed.total_seconds == 566
    assert parsed.start_time is not None
    assert parsed.completion_time is not None


def test_process_jenkins_event_message_inserts_build_and_audit(sqlite_engine) -> None:
    result = process_jenkins_event_message(sqlite_engine, _settings(), _finished_event_payload())

    assert result == "processed"

    with sqlite_engine.begin() as connection:
        build = connection.execute(
            text(
                """
                SELECT source_prow_job_id, state, job_name, normalized_build_url, repo_full_name, pr_number, is_pr_build
                FROM ci_l1_builds
                """
            )
        ).mappings().one()
        audit = connection.execute(
            text(
                """
                SELECT event_id, processing_status, normalized_build_url, result
                FROM ci_l1_jenkins_build_events
                """
            )
        ).mappings().one()

    assert build["source_prow_job_id"] is None
    assert build["state"] == "failure"
    assert build["job_name"] == "pingcap/tidb/ghpr_unit_test"
    assert build["normalized_build_url"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    assert build["repo_full_name"] == "pingcap/tidb"
    assert build["pr_number"] == 301
    assert build["is_pr_build"] == 1

    assert audit["event_id"] == "evt-1"
    assert audit["processing_status"] == "PROCESSED"
    assert audit["normalized_build_url"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    assert audit["result"] == "FAILURE"


def test_process_jenkins_event_message_submits_timings_after_build_write(sqlite_engine) -> None:
    enricher = _NoopTimingsEnricher()

    result = process_jenkins_event_message(
        sqlite_engine,
        _settings(),
        _finished_event_payload(),
        timings_enricher=enricher,
    )

    assert result == "processed"
    assert len(enricher.calls) == 1
    build_id, build_url = enricher.calls[0]
    assert build_id > 0
    assert build_url.endswith("/ghpr_unit_test/301/")


def test_process_jenkins_event_message_ignores_timings_submit_failure(sqlite_engine) -> None:
    class _FailingEnricher:
        def submit(self, *, build_id: int, build_url: str) -> bool:
            del build_id, build_url
            raise RuntimeError("queue unavailable")

    result = process_jenkins_event_message(
        sqlite_engine,
        _settings(),
        _finished_event_payload(),
        timings_enricher=_FailingEnricher(),
    )

    assert result == "processed"
    with sqlite_engine.begin() as connection:
        status = connection.execute(
            text("SELECT processing_status FROM ci_l1_jenkins_build_events")
        ).scalar_one()
    assert status == "PROCESSED"


def test_process_jenkins_event_message_inserts_real_plugin_payload(sqlite_engine) -> None:
    result = process_jenkins_event_message(sqlite_engine, _settings(), _real_jenkins_plugin_finished_event_payload())

    assert result == "processed"

    with sqlite_engine.begin() as connection:
        build = connection.execute(
            text(
                """
                SELECT source_prow_job_id, state, job_name, normalized_build_url, job_type, repo_full_name, pr_number, author, head_sha, build_system, cloud_phase
                FROM ci_l1_builds
                """
            )
        ).mappings().one()
        audit = connection.execute(
            text(
                """
                SELECT event_id, processing_status, normalized_build_url, result
                FROM ci_l1_jenkins_build_events
                """
            )
        ).mappings().one()

    assert build["source_prow_job_id"] == "3bfe389c-e361-4a6d-a1d6-679fb87b0e13"
    assert build["state"] == "success"
    assert build["job_name"] == "pingcap/tidb/pull_mysql_client_test"
    assert build["normalized_build_url"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/"
    assert build["job_type"] == "presubmit"
    assert build["repo_full_name"] == "pingcap/tidb"
    assert build["pr_number"] == 65626
    assert build["author"] == "terry1purcell"
    assert build["head_sha"] == "e6030436c2093b30da167ca295887b8df8eaeb07"
    assert build["build_system"] == "JENKINS"
    assert build["cloud_phase"] == "GCP"
    assert audit["event_id"] == "evt-real-1"
    assert audit["processing_status"] == "PROCESSED"
    assert audit["normalized_build_url"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/"
    assert audit["result"] == "SUCCESS"


def test_process_jenkins_event_message_reclassifies_legacy_idc_rows_from_event_url(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, base_ref, pr_number, is_pr_build,
                  context, url, normalized_build_url, author, start_time, completion_time,
                  total_seconds, head_sha, target_branch, cloud_phase, build_system
                ) VALUES (
                  301, 'prow-job-301', 'prow', 'ghpr_unit_test', 'presubmit', 'failure',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb', 'master', 301, 1,
                  'unit', 'https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/display/redirect',
                  'https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/',
                  'alice', '2026-04-24 10:00:00', '2026-04-24 10:20:00',
                  1200, '0123456789abcdef0123456789abcdef01234567', 'master', 'IDC', 'JENKINS'
                )
                """
            )
        )

    payload = _finished_event_payload()
    payload["subject"] = "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    result = process_jenkins_event_message(sqlite_engine, _settings(), payload)

    assert result == "processed"

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT source_prow_row_id, source_prow_job_id, namespace, job_name, job_type,
                           repo_full_name, state, cloud_phase
                    FROM ci_l1_builds
                    """
                )
            ).mappings()
        )

    assert len(rows) == 1
    row = rows[0]
    assert row["source_prow_row_id"] == 301
    assert row["source_prow_job_id"] == "prow-job-301"
    assert row["namespace"] == "prow"
    assert row["job_name"] == "pingcap/tidb/ghpr_unit_test"
    assert row["job_type"] == "presubmit"
    assert row["repo_full_name"] == "pingcap/tidb"
    assert row["state"] == "failure"
    assert row["cloud_phase"] == "TENCENT"


def test_process_jenkins_event_message_uses_prow_job_id_to_resolve_duplicate_build_urls(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, base_ref, pr_number, is_pr_build,
                  context, url, normalized_build_url, author, start_time, completion_time,
                  total_seconds, head_sha, target_branch, cloud_phase, build_system
                ) VALUES
                (
                  111, 'different-prow-job', 'prow', 'pull_mysql_client_test', 'presubmit', 'aborted',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb', 'master', 65626, 1,
                  'unit', 'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/display/redirect',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/',
                  'alice', '2026-04-27 13:00:00', '2026-04-27 13:05:00',
                  300, 'deadbeef', 'master', 'GCP', 'JENKINS'
                ),
                (
                  222, '3bfe389c-e361-4a6d-a1d6-679fb87b0e13', 'prow', 'pull_mysql_client_test', 'presubmit', 'pending',
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb', 'master', 65626, 1,
                  'unit', 'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/display/redirect',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/',
                  'terry1purcell', '2026-04-27 13:00:00', NULL,
                  NULL, 'e6030436c2093b30da167ca295887b8df8eaeb07', 'master', 'TENCENT', 'UNKNOWN'
                )
                """
            )
        )

    result = process_jenkins_event_message(sqlite_engine, _settings(), _real_jenkins_plugin_finished_event_payload())

    assert result == "processed"

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT source_prow_row_id, source_prow_job_id, state, completion_time, build_system, cloud_phase
                    FROM ci_l1_builds
                    ORDER BY source_prow_row_id
                    """
                )
            ).mappings()
        )

    assert len(rows) == 2
    assert rows[0]["source_prow_job_id"] == "different-prow-job"
    assert rows[0]["state"] == "aborted"
    assert rows[1]["source_prow_job_id"] == "3bfe389c-e361-4a6d-a1d6-679fb87b0e13"
    assert rows[1]["state"] == "success"
    assert rows[1]["completion_time"] is not None
    assert rows[1]["build_system"] == "JENKINS"
    assert rows[1]["cloud_phase"] == "GCP"


@pytest.mark.parametrize(
    "existing_url,event_url_kind",
    [
        ("https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1817/", "relative"),
        ("http://jenkins.jenkins.svc.cluster.local/job/pingcap/job/tidb/job/pull_mysql_client_test/1817/", "internal"),
        ("https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/another_job/1816/", "relative"),
        ("https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/", "absolute"),
        ("https://prow.tidb.net/jenkins-staging/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/", "relative"),
        ("https://prow.tidb.net/view/gs/prow-tidb-logs/1816/", "relative"),
        ("https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1817/", "relative"),
        ("https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/another_job/1816/", "internal"),
        ("https://do.pingcap.net/jenkins-staging/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/", "relative"),
        (None, "relative"),
        ("", "relative"),
        ("not-a-build-url", "relative"),
    ],
)
@pytest.mark.parametrize("has_url_fallback", [False, True])
def test_jenkins_event_rejects_prow_id_url_conflict_without_build_writes(
    sqlite_engine, existing_url: str | None, event_url_kind: str, has_url_fallback: bool,
) -> None:
    payload = _real_jenkins_plugin_finished_event_payload()
    if event_url_kind == "absolute":
        # An explicit GCP controller must still conflict with the same path on Tencent.
        payload["subject"] = worker.normalize_build_url(payload["source"])
    elif event_url_kind == "internal":
        payload["source"] = f"http://jenkins.jenkins.svc.cluster.local/{payload['source']}"
    parsed = parse_jenkins_finished_event(payload, _settings())
    enricher = _NoopTimingsEnricher()
    with sqlite_engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO ci_l1_builds (source_prow_job_id, normalized_build_url, state)
                VALUES (:prow_id, :url, 'pending')
            """),
            {"prow_id": parsed.source_prow_job_id, "url": existing_url},
        )
        if has_url_fallback:
            connection.execute(
                text("""
                    INSERT INTO ci_l1_builds (normalized_build_url, state)
                    VALUES (:url, 'pending')
                """),
                {"url": parsed.normalized_build_url},
            )
        original_builds = connection.execute(
            text("SELECT * FROM ci_l1_builds ORDER BY id")
        ).mappings().all()

    for _ in range(2):
        assert process_jenkins_event_message(
            sqlite_engine, _settings(), payload, timings_enricher=enricher,
        ) == "failed"
        with sqlite_engine.begin() as connection:
            assert connection.execute(
                text("SELECT * FROM ci_l1_builds ORDER BY id")
            ).mappings().all() == original_builds
            audit = connection.execute(
                text("SELECT * FROM ci_l1_jenkins_build_events")
            ).mappings().one()
        assert audit["processing_status"] == "FAILED"
        assert audit["event_id"] == parsed.event_id
        assert audit["normalized_build_url"] == parsed.normalized_build_url
        assert "source_prow_job_id conflicts with the canonical build URL" in audit["last_error"]
        assert f"row_id={original_builds[0]['id']}" in audit["last_error"]
        assert f"source_prow_job_id={parsed.source_prow_job_id!r}" in audit["last_error"]
        assert f"event_url={parsed.normalized_build_url!r}" in audit["last_error"]
        assert f"stored_canonical_url={worker.normalize_build_url(existing_url)!r}" in audit["last_error"]
        assert enricher.calls == []

    # A trusted sync repairs the identity; replaying the same event can now succeed.
    with sqlite_engine.begin() as connection:
        connection.execute(
            text("UPDATE ci_l1_builds SET normalized_build_url = :url WHERE id = :id"),
            {"url": parsed.normalized_build_url, "id": original_builds[0]["id"]},
        )
    assert process_jenkins_event_message(
        sqlite_engine, _settings(), payload, timings_enricher=enricher,
    ) == "processed"
    with sqlite_engine.begin() as connection:
        builds = connection.execute(
            text("SELECT * FROM ci_l1_builds ORDER BY id")
        ).mappings().all()
        audit = connection.execute(
            text("SELECT * FROM ci_l1_jenkins_build_events")
        ).mappings().one()
    assert len(builds) == len(original_builds)
    assert builds[0]["id"] == original_builds[0]["id"]
    assert builds[0]["state"] == "success"
    assert builds[1:] == original_builds[1:]
    assert audit["event_id"] == parsed.event_id
    assert audit["processing_status"] == "PROCESSED"
    assert audit["last_error"] is None
    assert enricher.calls == [(original_builds[0]["id"], parsed.normalized_build_url)]


@pytest.mark.parametrize(
    "existing_url",
    [
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/",
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816",
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/display/redirect",
        "/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/",
        "http://jenkins.jenkins.svc.cluster.local/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/",
        "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/",
        "https://do.pingcap.net/jenkins-staging/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/",
    ],
)
@pytest.mark.parametrize("event_url_kind", ["absolute", "relative", "internal"])
def test_jenkins_event_accepts_matching_prow_id_with_canonical_url_alias(
    sqlite_engine, existing_url: str, event_url_kind: str,
) -> None:
    payload = _real_jenkins_plugin_finished_event_payload()
    canonical_url = worker.normalize_build_url(existing_url)
    event_path = urlsplit(canonical_url).path
    if event_url_kind == "absolute":
        payload["subject"] = canonical_url
    elif event_url_kind == "internal":
        payload["source"] = f"http://jenkins.jenkins.svc.cluster.local{event_path}"
    elif event_path.startswith("/jenkins-staging/"):
        payload["source"] = event_path
    # Otherwise keep the real plugin's unmodified, host-less source.
    parsed = parse_jenkins_finished_event(payload, _settings())
    enricher = _NoopTimingsEnricher()
    with sqlite_engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO ci_l1_builds (source_prow_job_id, normalized_build_url, state)
                VALUES (:prow_id, :url, 'pending')
            """),
            {"prow_id": parsed.source_prow_job_id, "url": existing_url},
        )
        build_id = connection.execute(text("SELECT id FROM ci_l1_builds")).scalar_one()

    assert process_jenkins_event_message(
        sqlite_engine, _settings(), payload, timings_enricher=enricher,
    ) == "processed"
    assert process_jenkins_event_message(
        sqlite_engine, _settings(), payload, timings_enricher=enricher,
    ) == "skipped"

    with sqlite_engine.begin() as connection:
        build = connection.execute(text("SELECT * FROM ci_l1_builds")).mappings().one()
        audit = connection.execute(
            text("SELECT * FROM ci_l1_jenkins_build_events")
        ).mappings().one()
    assert build["id"] == build_id
    assert build["state"] == "success"
    assert build["source_prow_job_id"] == parsed.source_prow_job_id
    assert build["normalized_build_url"] == canonical_url
    assert build["cloud_phase"] == worker.classify_cloud_phase(canonical_url)
    assert build["build_system"] == worker.classify_build_system(canonical_url)
    assert audit["processing_status"] == "PROCESSED"
    assert audit["normalized_build_url"] == canonical_url
    assert enricher.calls == [(build_id, canonical_url)]


def test_jenkins_event_does_not_treat_unknown_controller_as_gcp(sqlite_engine) -> None:
    payload = _real_jenkins_plugin_finished_event_payload()
    assert process_jenkins_event_message(sqlite_engine, _settings(), payload) == "processed"
    with sqlite_engine.begin() as connection:
        original = connection.execute(text("SELECT * FROM ci_l1_builds")).mappings().one()

    payload["id"] = "evt-unknown-controller"
    payload["subject"] = original["normalized_build_url"].replace(
        "prow.tidb.net", "unrelated-controller.example.net",
    )
    enricher = _NoopTimingsEnricher()
    assert process_jenkins_event_message(
        sqlite_engine, _settings(), payload, timings_enricher=enricher,
    ) == "failed"
    with sqlite_engine.begin() as connection:
        assert connection.execute(
            text("SELECT * FROM ci_l1_builds")
        ).mappings().one() == original
        audit = connection.execute(
            text("SELECT processing_status, last_error FROM ci_l1_jenkins_build_events WHERE event_id = :id"),
            {"id": payload["id"]},
        ).mappings().one()
    assert audit["processing_status"] == "FAILED"
    assert "source_prow_job_id conflicts" in audit["last_error"]
    assert enricher.calls == []


@pytest.mark.parametrize("has_prow_id", [False, True])
def test_jenkins_event_accepts_identical_delivery_with_unchanged_build(
    sqlite_engine, has_prow_id: bool,
) -> None:
    payload = (
        _real_jenkins_plugin_finished_event_payload() if has_prow_id else _finished_event_payload()
    )
    parsed = parse_jenkins_finished_event(payload, _settings())
    enricher = _NoopTimingsEnricher()
    assert process_jenkins_event_message(
        sqlite_engine, _settings(), payload, timings_enricher=enricher,
    ) == "processed"
    with sqlite_engine.begin() as connection:
        original = connection.execute(text("SELECT * FROM ci_l1_builds")).mappings().one()

    payload["id"] = "evt-identical-redelivery"
    assert process_jenkins_event_message(
        sqlite_engine, _settings(), payload, timings_enricher=enricher,
    ) == "processed"
    with sqlite_engine.begin() as connection:
        repeated = connection.execute(text("SELECT * FROM ci_l1_builds")).mappings().one()
        # updated_at is second-precision wall time, not part of the build identity.
        assert {key: value for key, value in repeated.items() if key != "updated_at"} == {
            key: value for key, value in original.items() if key != "updated_at"
        }
        audits = connection.execute(
            text("SELECT processing_status, last_error FROM ci_l1_jenkins_build_events")
        ).mappings().all()
    assert len(audits) == 2
    assert all(audit["processing_status"] == "PROCESSED" for audit in audits)
    assert all(audit["last_error"] is None for audit in audits)
    assert enricher.calls == [(original["id"], parsed.normalized_build_url)] * 2


@pytest.mark.parametrize("explicit_url", [False, True])
def test_production_mysql_engine_counts_matched_not_changed_rows(explicit_url: bool) -> None:
    settings = _settings()
    database = replace(
        settings.database,
        url="mysql+pymysql://dashboard@db.invalid/ci" if explicit_url else None,
        host="db.invalid", port=3306, user="dashboard", database="ci",
    )
    engine = build_engine(replace(settings, database=database))
    try:
        # Inspect the real SQLAlchemy dialect's DBAPI args without opening a connection.
        _args, kwargs = engine.dialect.create_connect_args(engine.url)
        assert kwargs["client_flag"] & CLIENT.FOUND_ROWS
        assert engine.dialect.supports_sane_rowcount
    finally:
        engine.dispose()


@pytest.mark.parametrize("changed_field", ["normalized_build_url", "source_prow_job_id"])
def test_jenkins_event_rejects_merge_target_changed_since_lookup(
    sqlite_engine, monkeypatch: pytest.MonkeyPatch, changed_field: str,
) -> None:
    payload = _real_jenkins_plugin_finished_event_payload()
    parsed = parse_jenkins_finished_event(payload, _settings())
    current_identity = {
        "normalized_build_url": parsed.normalized_build_url,
        "source_prow_job_id": parsed.source_prow_job_id,
    }
    current_identity[changed_field] = (
        parsed.normalized_build_url.replace("/1816/", "/1817/")
        if changed_field == "normalized_build_url" else "another-prow-job"
    )
    with sqlite_engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO ci_l1_builds (source_prow_job_id, normalized_build_url, state)
                VALUES (:source_prow_job_id, :normalized_build_url, 'pending')
            """),
            current_identity,
        )
        original = connection.execute(text("SELECT * FROM ci_l1_builds")).mappings().one()

    # Model a stale, previously matching read while the database identity has moved on.
    stale_target = {
        "id": original["id"],
        "source_prow_job_id": parsed.source_prow_job_id,
        "normalized_build_url": parsed.normalized_build_url,
    }
    monkeypatch.setattr(worker, "fetch_existing_build_targets", lambda *args, **kwargs: (
        {parsed.source_prow_job_id: stale_target},
        {parsed.normalized_build_url: [stale_target]},
    ))
    enricher = _NoopTimingsEnricher()
    assert process_jenkins_event_message(
        sqlite_engine, _settings(), payload, timings_enricher=enricher,
    ) == "failed"

    with sqlite_engine.begin() as connection:
        assert connection.execute(
            text("SELECT * FROM ci_l1_builds")
        ).mappings().one() == original
        audit = connection.execute(
            text("SELECT processing_status, last_error FROM ci_l1_jenkins_build_events")
        ).mappings().one()
    assert audit["processing_status"] == "FAILED"
    assert "merge target changed before update" in audit["last_error"]
    assert f"target_id={original['id']}" in audit["last_error"]
    assert f"matched_source_prow_job_id={stale_target['source_prow_job_id']!r}" in audit["last_error"]
    assert f"event_url={parsed.normalized_build_url!r}" in audit["last_error"]
    assert f"stored_canonical_url={parsed.normalized_build_url!r}" in audit["last_error"]
    assert enricher.calls == []


def test_process_jenkins_event_message_skips_duplicate_processed_event(sqlite_engine) -> None:
    first = process_jenkins_event_message(sqlite_engine, _settings(), _finished_event_payload())
    second = process_jenkins_event_message(sqlite_engine, _settings(), _finished_event_payload())

    assert first == "processed"
    assert second == "skipped"

    with sqlite_engine.begin() as connection:
        build_count = connection.execute(text("SELECT COUNT(*) FROM ci_l1_builds")).scalar_one()
        audit_count = connection.execute(text("SELECT COUNT(*) FROM ci_l1_jenkins_build_events")).scalar_one()

    assert build_count == 1
    assert audit_count == 1


def test_process_jenkins_event_message_marks_audited_failures_without_raising(sqlite_engine) -> None:
    payload = _finished_event_payload()
    payload["type"] = "dev.cdevents.pipelinerun.started.0.1.0"

    result = process_jenkins_event_message(sqlite_engine, _settings(), payload)

    assert result == "failed"

    with sqlite_engine.begin() as connection:
        build_count = connection.execute(text("SELECT COUNT(*) FROM ci_l1_builds")).scalar_one()
        audit = connection.execute(
            text(
                """
                SELECT event_id, event_type, processing_status, last_error
                FROM ci_l1_jenkins_build_events
                """
            )
        ).mappings().one()

    assert build_count == 0
    assert audit["event_id"] == "evt-1"
    assert audit["event_type"] == "dev.cdevents.pipelinerun.started.0.1.0"
    assert audit["processing_status"] == "FAILED"
    assert "unsupported CloudEvent type" in str(audit["last_error"])


def test_run_consume_jenkins_events_commits_audited_failures(sqlite_engine) -> None:
    class _Record:
        def __init__(self, value: bytes) -> None:
            self.value = value

    class _Consumer:
        def __init__(self) -> None:
            self._poll_count = 0
            self.commits = 0

        def poll(self, timeout_ms: int, max_records: int):
            del timeout_ms, max_records
            self._poll_count += 1
            if self._poll_count == 1:
                return {("jenkins-event", 0): [_Record(json.dumps(_finished_event_payload()).encode("utf-8"))]}
            if self._poll_count == 2:
                failed_payload = _finished_event_payload(event_id="evt-2")
                failed_payload["type"] = "dev.cdevents.pipelinerun.started.0.1.0"
                return {("jenkins-event", 0): [_Record(json.dumps(failed_payload).encode("utf-8"))]}
            return {}

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            return None

    consumer = _Consumer()
    summary = run_consume_jenkins_events(
        sqlite_engine,
        _settings(),
        max_messages=2,
        consumer=consumer,
        timings_enricher=_NoopTimingsEnricher(),
    )

    assert summary.messages_polled == 2
    assert summary.events_processed == 1
    assert summary.events_failed == 1
    assert summary.events_skipped == 0
    assert summary.build_rows_written == 1
    assert consumer.commits == 2


def test_run_consume_jenkins_events_respects_max_messages_across_partitions(sqlite_engine) -> None:
    class _Record:
        def __init__(self, value: bytes) -> None:
            self.value = value

    class _Consumer:
        def __init__(self) -> None:
            self.commits = 0

        def poll(self, timeout_ms: int, max_records: int):
            del timeout_ms, max_records
            return {
                ("jenkins-event", 0): [_Record(json.dumps(_finished_event_payload(event_id="evt-10")).encode("utf-8"))],
                ("jenkins-event", 1): [_Record(json.dumps(_finished_event_payload(event_id="evt-11")).encode("utf-8"))],
            }

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            return None

    consumer = _Consumer()
    summary = run_consume_jenkins_events(
        sqlite_engine,
        _settings(),
        max_messages=1,
        consumer=consumer,
        timings_enricher=_NoopTimingsEnricher(),
    )

    assert summary.messages_polled == 1
    assert summary.events_processed == 1
    assert summary.events_failed == 0
    assert summary.build_rows_written == 1
    assert consumer.commits == 1


def test_build_kafka_consumer_uses_earliest_offset(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeKafkaConsumer:
        def __init__(self, *args, **kwargs) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

    import sys
    import types

    monkeypatch.setitem(sys.modules, "kafka", types.SimpleNamespace(KafkaConsumer=_FakeKafkaConsumer))

    settings = Settings(
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
        kafka=KafkaSettings(bootstrap_servers=("kafka-a:9092",)),
        log_level="INFO",
    )

    _build_kafka_consumer(settings, topic="jenkins-event", group_id="ci-dashboard-test")

    assert captured["args"] == ("jenkins-event",)
    assert captured["kwargs"]["auto_offset_reset"] == "earliest"
    assert captured["kwargs"]["group_id"] == "ci-dashboard-test"


def test_map_jenkins_result_to_state() -> None:
    assert map_jenkins_result_to_state("SUCCESS") == "success"
    assert map_jenkins_result_to_state("FAILURE") == "failure"
    assert map_jenkins_result_to_state("UNSTABLE") == "failure"
    assert map_jenkins_result_to_state("ABORTED") == "aborted"
    assert map_jenkins_result_to_state("NOT_BUILT") == "error"
    assert map_jenkins_result_to_state(None) == "error"
