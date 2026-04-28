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


def test_parse_jenkins_finished_event_extracts_canonical_fields() -> None:
    parsed = parse_jenkins_finished_event(_finished_event_payload(), _settings())

    assert parsed.event_id == "evt-1"
    assert parsed.normalized_build_url == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    assert parsed.build_url == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    assert parsed.state == "failure"
    assert parsed.jenkins_result == "FAILURE"
    assert parsed.job_name == "ghpr_unit_test"
    assert parsed.repo_full_name == "pingcap/tidb"
    assert parsed.pr_number == 301
    assert parsed.is_pr_build is True
    assert parsed.target_branch == "master"
    assert parsed.total_seconds == 1200
    assert parsed.start_time == datetime(2026, 4, 24, 10, 0, 0)
    assert parsed.completion_time == datetime(2026, 4, 24, 10, 20, 0)


def test_process_jenkins_event_message_inserts_build_and_audit(sqlite_engine) -> None:
    result = process_jenkins_event_message(sqlite_engine, _settings(), _finished_event_payload())

    assert result == "processed"

    with sqlite_engine.begin() as connection:
        build = connection.execute(
            text(
                """
                SELECT source_prow_job_id, state, normalized_build_url, repo_full_name, pr_number, is_pr_build
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
    assert build["normalized_build_url"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    assert build["repo_full_name"] == "pingcap/tidb"
    assert build["pr_number"] == 301
    assert build["is_pr_build"] == 1

    assert audit["event_id"] == "evt-1"
    assert audit["processing_status"] == "PROCESSED"
    assert audit["normalized_build_url"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    assert audit["result"] == "FAILURE"


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
    assert row["job_name"] == "ghpr_unit_test"
    assert row["job_type"] == "presubmit"
    assert row["repo_full_name"] == "pingcap/tidb"
    assert row["state"] == "failure"


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
