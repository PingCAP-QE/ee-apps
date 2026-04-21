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
            "CI_DASHBOARD_LOG_LEVEL": "debug",
        }
    )

    assert settings.database.url == "sqlite+pysqlite:///tmp/example.db"
    assert settings.database.host is None
    assert settings.jobs.batch_size == 12
    assert settings.jobs.refresh_group_batch_size == 7
    assert settings.jobs.refresh_build_limit == 3456
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
