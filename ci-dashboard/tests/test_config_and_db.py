from __future__ import annotations

import logging

import pytest

from ci_dashboard.common.config import get_settings, load_settings
from ci_dashboard.common.db import build_engine
from ci_dashboard.common.logging import configure_logging


def test_load_settings_supports_db_url() -> None:
    settings = load_settings(
        {
            "CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///tmp/example.db",
            "CI_DASHBOARD_BATCH_SIZE": "12",
            "CI_DASHBOARD_REFRESH_GROUP_BATCH_SIZE": "7",
            "CI_DASHBOARD_REFRESH_BUILD_LIMIT": "3456",
            "CI_DASHBOARD_KAFKA_BOOTSTRAP_SERVERS": "kafka-a:9092,kafka-b:9092",
            "CI_DASHBOARD_KAFKA_JENKINS_EVENTS_TOPIC": "jenkins-event-test",
            "CI_DASHBOARD_KAFKA_JENKINS_GROUP_ID": "ci-dashboard-test",
            "CI_DASHBOARD_KAFKA_POLL_TIMEOUT_MS": "2500",
            "CI_DASHBOARD_JENKINS_INTERNAL_BASE_URL": "http://jenkins.jenkins.svc.cluster.local:80",
            "CI_DASHBOARD_JENKINS_USERNAME": "robot",
            "CI_DASHBOARD_JENKINS_API_TOKEN": "token-1",
            "CI_DASHBOARD_JENKINS_HTTP_TIMEOUT_SECONDS": "45",
            "CI_DASHBOARD_JENKINS_PROGRESSIVE_PROBE_START": "999999",
            "CI_DASHBOARD_ARCHIVE_BUILD_LIMIT": "77",
            "CI_DASHBOARD_ARCHIVE_LOG_TAIL_BYTES": "65536",
            "CI_DASHBOARD_GCS_BUCKET": "ci-dashboard-prod",
            "CI_DASHBOARD_GCS_PREFIX": "ci-dashboard/v3/custom-prefix",
            "CI_DASHBOARD_LOG_LEVEL": "debug",
        }
    )

    assert settings.database.url == "sqlite+pysqlite:///tmp/example.db"
    assert settings.database.host is None
    assert settings.jobs.batch_size == 12
    assert settings.jobs.refresh_group_batch_size == 7
    assert settings.jobs.refresh_build_limit == 3456
    assert settings.kafka.bootstrap_servers == ("kafka-a:9092", "kafka-b:9092")
    assert settings.kafka.jenkins_events_topic == "jenkins-event-test"
    assert settings.kafka.jenkins_consumer_group == "ci-dashboard-test"
    assert settings.kafka.poll_timeout_ms == 2500
    assert settings.jenkins.internal_base_url == "http://jenkins.jenkins.svc.cluster.local:80"
    assert settings.jenkins.username == "robot"
    assert settings.jenkins.api_token == "token-1"
    assert settings.jenkins.http_timeout_seconds == 45
    assert settings.jenkins.progressive_probe_start == 999999
    assert settings.archive.build_limit == 77
    assert settings.archive.log_tail_bytes == 65536
    assert settings.archive.gcs_bucket == "ci-dashboard-prod"
    assert settings.archive.gcs_prefix == "ci-dashboard/v3/custom-prefix"
    assert settings.log_level == "DEBUG"


def test_load_settings_requires_tidb_fields_without_db_url() -> None:
    with pytest.raises(ValueError, match="TIDB_HOST"):
        load_settings({"CI_DASHBOARD_BATCH_SIZE": "10"})


def test_load_settings_rejects_invalid_integer() -> None:
    with pytest.raises(
        ValueError,
        match=r"CI_DASHBOARD_BATCH_SIZE must be an integer, got 'abc'",
    ):
        load_settings(
            {
                "CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///tmp/example.db",
                "CI_DASHBOARD_BATCH_SIZE": "abc",
            }
        )


def test_load_settings_rejects_invalid_refresh_group_batch_size() -> None:
    with pytest.raises(
        ValueError,
        match=r"CI_DASHBOARD_REFRESH_GROUP_BATCH_SIZE must be an integer, got 'abc'",
    ):
        load_settings(
            {
                "CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///tmp/example.db",
                "CI_DASHBOARD_REFRESH_GROUP_BATCH_SIZE": "abc",
            }
        )


def test_load_settings_rejects_invalid_refresh_build_limit() -> None:
    with pytest.raises(
        ValueError,
        match=r"CI_DASHBOARD_REFRESH_BUILD_LIMIT must be an integer, got 'abc'",
    ):
        load_settings(
            {
                "CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///tmp/example.db",
                "CI_DASHBOARD_REFRESH_BUILD_LIMIT": "abc",
            }
        )


def test_load_settings_rejects_non_positive_refresh_build_limit() -> None:
    with pytest.raises(
        ValueError,
        match=r"CI_DASHBOARD_REFRESH_BUILD_LIMIT must be positive, got '0'",
    ):
        load_settings(
            {
                "CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///tmp/example.db",
                "CI_DASHBOARD_REFRESH_BUILD_LIMIT": "0",
            }
        )


def test_load_settings_rejects_invalid_kafka_poll_timeout() -> None:
    with pytest.raises(
        ValueError,
        match=r"CI_DASHBOARD_KAFKA_POLL_TIMEOUT_MS must be an integer, got 'abc'",
    ):
        load_settings(
            {
                "CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///tmp/example.db",
                "CI_DASHBOARD_KAFKA_POLL_TIMEOUT_MS": "abc",
            }
        )


def test_load_settings_rejects_invalid_archive_limit() -> None:
    with pytest.raises(
        ValueError,
        match=r"CI_DASHBOARD_ARCHIVE_BUILD_LIMIT must be positive, got '0'",
    ):
        load_settings(
            {
                "CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///tmp/example.db",
                "CI_DASHBOARD_ARCHIVE_BUILD_LIMIT": "0",
            }
        )


def test_build_engine_supports_sqlite_url() -> None:
    settings = load_settings({"CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///:memory:"})
    engine = build_engine(settings)
    assert engine.dialect.name == "sqlite"


def test_configure_logging_sets_root_level() -> None:
    configure_logging("WARNING")
    assert logging.getLogger().level in {0, logging.WARNING}


def test_get_settings_reads_environment_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("CI_DASHBOARD_DB_URL", "sqlite+pysqlite:///:memory:")
    settings = get_settings()
    assert settings.database.url == "sqlite+pysqlite:///:memory:"
    get_settings.cache_clear()
