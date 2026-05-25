from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping


def _read_required(environ: Mapping[str, str], key: str) -> str:
    value = environ.get(key)
    if value is None or value.strip() == "":
        raise ValueError(f"Missing required environment variable: {key}")
    return value


def _read_int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{key} must be positive, got {raw!r}")
    return value


@dataclass(frozen=True)
class DatabaseSettings:
    url: str | None
    host: str | None
    port: int | None
    user: str | None
    password: str | None
    database: str | None
    ssl_ca: str | None


@dataclass(frozen=True)
class LarkSettings:
    app_id: str | None = None
    app_secret: str | None = None
    github_custom_attr_id: str | None = None
    notify_open_id: str | None = None
    root_department_id: str = "0"

    @property
    def is_configured(self) -> bool:
        return bool(self.app_id and self.app_secret)


@dataclass(frozen=True)
class Settings:
    database: DatabaseSettings
    lark: LarkSettings = LarkSettings()
    log_level: str = "INFO"


def load_settings(
    environ: Mapping[str, str] | None = None,
    *,
    require_database: bool = True,
) -> Settings:
    env = os.environ if environ is None else environ
    database_url = env.get("ROSTER_DB_URL") or env.get("CI_DASHBOARD_DB_URL") or None
    if database_url:
        database = DatabaseSettings(
            url=database_url,
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=env.get("ROSTER_TIDB_SSL_CA") or env.get("TIDB_SSL_CA") or None,
        )
    elif not require_database:
        database = DatabaseSettings(
            url=None,
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=env.get("ROSTER_TIDB_SSL_CA") or env.get("TIDB_SSL_CA") or None,
        )
    else:
        database = DatabaseSettings(
            url=None,
            host=_read_required_any(env, "ROSTER_TIDB_HOST", "TIDB_HOST"),
            port=_read_int_any(env, ("ROSTER_TIDB_PORT", "TIDB_PORT"), 4000),
            user=_read_required_any(env, "ROSTER_TIDB_USER", "TIDB_USER"),
            password=_read_required_any(env, "ROSTER_TIDB_PASSWORD", "TIDB_PASSWORD"),
            database=_read_required_any(env, "ROSTER_TIDB_DB", "TIDB_DB"),
            ssl_ca=env.get("ROSTER_TIDB_SSL_CA") or env.get("TIDB_SSL_CA") or None,
        )
    return Settings(
        database=database,
        lark=LarkSettings(
            app_id=env.get("ROSTER_LARK_APP_ID") or None,
            app_secret=env.get("ROSTER_LARK_APP_SECRET") or None,
            github_custom_attr_id=env.get("ROSTER_LARK_GITHUB_CUSTOM_ATTR_ID") or None,
            notify_open_id=env.get("ROSTER_LARK_NOTIFY_OPEN_ID") or None,
            root_department_id=env.get("ROSTER_LARK_ROOT_DEPARTMENT_ID") or "0",
        ),
        log_level=(env.get("ROSTER_LOG_LEVEL") or "INFO").upper(),
    )


@lru_cache(maxsize=1)
def get_settings(require_database: bool = True) -> Settings:
    return load_settings(require_database=require_database)


def _read_required_any(environ: Mapping[str, str], *keys: str) -> str:
    for key in keys:
        value = environ.get(key)
        if value is not None and value.strip() != "":
            return value
    raise ValueError(f"Missing required environment variable: {' or '.join(keys)}")


def _read_int_any(environ: Mapping[str, str], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        if key in environ:
            return _read_int(environ, key, default)
    return default
