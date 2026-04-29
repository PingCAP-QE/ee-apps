from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from ci_dashboard.common.config import get_settings, load_settings
from ci_dashboard.common.db import _build_connect_args, build_engine, install_sqlite_functions
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
            "CI_DASHBOARD_GCS_PREFIX": "ci-dashboard/custom-prefix",
            "CI_DASHBOARD_LLM_PROVIDER": "noop",
            "CI_DASHBOARD_LLM_MODEL": "gemini-2.5-pro",
            "CI_DASHBOARD_LLM_API_KEY": "test-key",
            "CI_DASHBOARD_LLM_BASE_URL": "https://api-vip.codex-for.me/v1",
            "CI_DASHBOARD_LLM_REASONING_EFFORT": "high",
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
    assert settings.archive.gcs_prefix == "ci-dashboard/custom-prefix"
    assert settings.llm.provider == "noop"
    assert settings.llm.model == "gemini-2.5-pro"
    assert settings.llm.api_key == "test-key"
    assert settings.llm.base_url == "https://api-vip.codex-for.me/v1"
    assert settings.llm.reasoning_effort == "high"
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
    with engine.begin() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT normalize_build_url('https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/1/display/redirect')"
            ).scalar_one()
            == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/1/"
        )
        assert (
            connection.exec_driver_sql(
                "SELECT normalized_job_path_from_key('https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/1/')"
            ).scalar_one()
            == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/"
        )


def test_build_connect_args_supports_optional_ssl_ca() -> None:
    without_ssl = load_settings({"CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///:memory:"})
    assert _build_connect_args(without_ssl.database) == {}

    with_ssl = load_settings(
        {
            "TIDB_HOST": "db.example.com",
            "TIDB_PORT": "4000",
            "TIDB_USER": "ci",
            "TIDB_PASSWORD": "secret",
            "TIDB_DB": "dashboard",
            "TIDB_SSL_CA": "/etc/certs/ca.pem",
        }
    )
    assert _build_connect_args(with_ssl.database) == {"ssl": {"ca": "/etc/certs/ca.pem"}}


def test_install_sqlite_functions_is_noop_for_non_sqlite_engine() -> None:
    install_sqlite_functions(SimpleNamespace(dialect=SimpleNamespace(name="mysql")))


def test_build_engine_builds_mysql_url_and_ssl_connect_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    install_calls: list[object] = []
    monkeypatch.setattr("ci_dashboard.common.db.create_engine", fake_create_engine)
    monkeypatch.setattr("ci_dashboard.common.db.install_sqlite_functions", install_calls.append)

    settings = load_settings(
        {
            "TIDB_HOST": "db.example.com",
            "TIDB_PORT": "4000",
            "TIDB_USER": "ci",
            "TIDB_PASSWORD": "secret",
            "TIDB_DB": "dashboard",
            "TIDB_SSL_CA": "/etc/certs/ca.pem",
        }
    )

    engine = build_engine(settings)

    assert engine.dialect.name == "mysql"
    assert str(captured["url"]) == "mysql+pymysql://ci:***@db.example.com:4000/dashboard?charset=utf8mb4"
    assert captured["kwargs"] == {
        "pool_pre_ping": True,
        "future": True,
        "connect_args": {"ssl": {"ca": "/etc/certs/ca.pem"}},
    }
    assert install_calls == [engine]


def test_configure_logging_sets_root_level() -> None:
    configure_logging("WARNING")
    assert logging.getLogger().level in {0, logging.WARNING}


def test_get_settings_reads_environment_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("CI_DASHBOARD_DB_URL", "sqlite+pysqlite:///:memory:")
    settings = get_settings()
    assert settings.database.url == "sqlite+pysqlite:///:memory:"
    get_settings.cache_clear()
