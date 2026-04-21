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
class JobSettings:
    batch_size: int = 1000
    refresh_group_batch_size: int = 25
    refresh_build_limit: int = 5000


@dataclass(frozen=True)
class Settings:
    database: DatabaseSettings
    jobs: JobSettings
    log_level: str = "INFO"


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    database_url = env.get("CI_DASHBOARD_DB_URL") or None
    if database_url:
        database = DatabaseSettings(
            url=database_url,
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=env.get("TIDB_SSL_CA") or None,
        )
    else:
        database = DatabaseSettings(
            url=None,
            host=_read_required(env, "TIDB_HOST"),
            port=_read_int(env, "TIDB_PORT", 4000),
            user=_read_required(env, "TIDB_USER"),
            password=_read_required(env, "TIDB_PASSWORD"),
            database=_read_required(env, "TIDB_DB"),
            ssl_ca=env.get("TIDB_SSL_CA") or None,
        )
    return Settings(
        database=database,
        jobs=JobSettings(
            batch_size=_read_int(env, "CI_DASHBOARD_BATCH_SIZE", 1000),
            refresh_group_batch_size=_read_int(
                env,
                "CI_DASHBOARD_REFRESH_GROUP_BATCH_SIZE",
                25,
            ),
            refresh_build_limit=_read_int(
                env,
                "CI_DASHBOARD_REFRESH_BUILD_LIMIT",
                5000,
            ),
        ),
        log_level=(env.get("CI_DASHBOARD_LOG_LEVEL") or "INFO").upper(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
