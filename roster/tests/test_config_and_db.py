from __future__ import annotations

import pytest

from roster.common.config import load_settings
from roster.common.db import build_engine


def test_load_settings_uses_roster_db_url() -> None:
    settings = load_settings({"ROSTER_DB_URL": "sqlite:///:memory:"})

    assert settings.database.url == "sqlite:///:memory:"
    assert settings.database.host is None
    assert settings.lark.app_id is None
    assert settings.lark.app_secret is None


def test_load_settings_builds_tidb_fields() -> None:
    settings = load_settings(
        {
            "ROSTER_TIDB_HOST": "tidb.example.com",
            "ROSTER_TIDB_PORT": "4000",
            "ROSTER_TIDB_USER": "roster",
            "ROSTER_TIDB_PASSWORD": "secret",
            "ROSTER_TIDB_DB": "insight",
            "ROSTER_TIDB_SSL_CA": "/var/run/ca.crt",
            "ROSTER_LOG_LEVEL": "debug",
            "ROSTER_LARK_APP_ID": "cli_xxx",
            "ROSTER_LARK_APP_SECRET": "secret",
            "ROSTER_LARK_GITHUB_CUSTOM_ATTR_ID": "github_attr",
            "ROSTER_LARK_NOTIFY_OPEN_ID": "ou_xxx",
            "ROSTER_LARK_NOTIFY_OPEN_IDS": "ou_xxx, ou_yyy",
            "ROSTER_LARK_ROOT_DEPARTMENT_ID": "od-root",
        }
    )

    assert settings.database.host == "tidb.example.com"
    assert settings.database.port == 4000
    assert settings.database.user == "roster"
    assert settings.database.password == "secret"
    assert settings.database.database == "insight"
    assert settings.database.ssl_ca == "/var/run/ca.crt"
    assert settings.lark.app_id == "cli_xxx"
    assert settings.lark.app_secret == "secret"
    assert settings.lark.github_custom_attr_id == "github_attr"
    assert settings.lark.notify_open_id == "ou_xxx"
    assert settings.lark.notify_open_ids == ("ou_xxx", "ou_yyy")
    assert settings.lark.all_notify_open_ids == ("ou_xxx", "ou_yyy")
    assert settings.lark.has_notify_targets is True
    assert settings.lark.root_department_id == "od-root"
    assert settings.log_level == "DEBUG"


def test_load_settings_accepts_ci_dashboard_tidb_secret_keys() -> None:
    settings = load_settings(
        {
            "TIDB_HOST": "tidb.example.com",
            "TIDB_PORT": "4000",
            "TIDB_USER": "roster",
            "TIDB_PASSWORD": "secret",
            "TIDB_DB": "insight",
            "TIDB_SSL_CA": "/var/run/ca.crt",
        }
    )

    assert settings.database.host == "tidb.example.com"
    assert settings.database.port == 4000
    assert settings.database.user == "roster"
    assert settings.database.password == "secret"
    assert settings.database.database == "insight"
    assert settings.database.ssl_ca == "/var/run/ca.crt"


def test_load_settings_accepts_ci_dashboard_db_url_key() -> None:
    settings = load_settings({"CI_DASHBOARD_DB_URL": "sqlite:///:memory:"})

    assert settings.database.url == "sqlite:///:memory:"


def test_load_settings_can_skip_database_for_lark_only_commands() -> None:
    settings = load_settings(
        {
            "ROSTER_LARK_APP_ID": "cli_xxx",
            "ROSTER_LARK_APP_SECRET": "secret",
        },
        require_database=False,
    )

    assert settings.database.url is None
    assert settings.database.host is None
    assert settings.lark.is_configured is True


def test_lark_settings_are_disabled_when_partially_configured() -> None:
    settings = load_settings(
        {
            "ROSTER_DB_URL": "sqlite:///:memory:",
            "ROSTER_LARK_APP_ID": "cli_xxx",
        }
    )

    assert settings.lark.is_configured is False


def test_load_settings_requires_tidb_fields_without_url() -> None:
    with pytest.raises(ValueError, match="ROSTER_TIDB_HOST"):
        load_settings({})


def test_build_engine_uses_sqlite_url() -> None:
    settings = load_settings({"ROSTER_DB_URL": "sqlite:///:memory:"})
    engine = build_engine(settings)

    assert engine.dialect.name == "sqlite"


def test_build_engine_builds_mysql_url() -> None:
    settings = load_settings(
        {
            "ROSTER_TIDB_HOST": "tidb.example.com",
            "ROSTER_TIDB_PORT": "4000",
            "ROSTER_TIDB_USER": "roster",
            "ROSTER_TIDB_PASSWORD": "secret",
            "ROSTER_TIDB_DB": "insight",
        }
    )
    engine = build_engine(settings)

    assert engine.dialect.name == "mysql"
    assert str(engine.url) == "mysql+pymysql://roster:***@tidb.example.com:4000/insight?charset=utf8mb4"
