from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, KafkaSettings, Settings
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


def test_process_jenkins_event_message_inserts_real_plugin_payload(sqlite_engine) -> None:
    result = process_jenkins_event_message(sqlite_engine, _settings(), _real_jenkins_plugin_finished_event_payload())

    assert result == "processed"

    with sqlite_engine.begin() as connection:
        build = connection.execute(
            text(
                """
                SELECT source_prow_job_id, state, job_name, url, normalized_build_url, job_type, repo_full_name, pr_number, author, head_sha, build_system, cloud_phase
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
    assert build["url"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull_mysql_client_test/1816/"
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


def test_process_jenkins_event_message_enriches_existing_prow_row_without_clearing_it(sqlite_engine) -> None:
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
                  'unit', 'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/display/redirect',
                  'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/',
                  'alice', '2026-04-24 10:00:00', '2026-04-24 10:20:00',
                  1200, '0123456789abcdef0123456789abcdef01234567', 'master', 'GCP', 'JENKINS'
                )
                """
            )
        )

    result = process_jenkins_event_message(sqlite_engine, _settings(), _finished_event_payload())

    assert result == "processed"

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT source_prow_row_id, source_prow_job_id, namespace, job_name, job_type,
                           repo_full_name, state
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
                  NULL, 'e6030436c2093b30da167ca295887b8df8eaeb07', 'master', 'IDC', 'UNKNOWN'
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
