from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Mapping

DEFAULT_GCP_BILLING_TABLE = (
    "gcp-digital-bi.gcp_billing_detailed."
    "gcp_billing_export_resource_v1_01D088_8F9CF2_8AF1C6"
)
DEFAULT_GCP_ACCOUNT_ID = "pingcap-testing-account"
DEFAULT_AWS_BILLING_TABLE = "gcp-digital-bi.stg_cloud_billing.stg_aws_billing"
DEFAULT_EARLIEST_USAGE_DATE = date(2026, 1, 1)


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
class GcpBillingSettings:
    billing_table: str = DEFAULT_GCP_BILLING_TABLE
    account_id: str = DEFAULT_GCP_ACCOUNT_ID
    earliest_usage_date: date = DEFAULT_EARLIEST_USAGE_DATE
    sync_overlap_days: int = 3
    sync_lag_days: int = 5
    export_overlap_days: int = 0
    sync_initial_lookback_days: int | None = None
    unmatched_resource_lag_days: int = 5
    page_size: int = 5000


@dataclass(frozen=True)
class AwsBillingSettings:
    billing_table: str = DEFAULT_AWS_BILLING_TABLE
    account_id: str | None = None
    earliest_usage_date: date = DEFAULT_EARLIEST_USAGE_DATE
    export_overlap_months: int = 1
    sync_initial_lookback_months: int | None = 2
    page_size: int = 5000


@dataclass(frozen=True)
class Settings:
    database: DatabaseSettings
    gcp_billing: GcpBillingSettings = GcpBillingSettings()
    aws_billing: AwsBillingSettings = AwsBillingSettings()
    log_level: str = "INFO"


def load_settings(
    environ: Mapping[str, str] | None = None,
    *,
    require_database: bool = True,
) -> Settings:
    env = os.environ if environ is None else environ
    database = _load_database_settings(env, require_database=require_database)
    return Settings(
        database=database,
        gcp_billing=GcpBillingSettings(
            billing_table=_read_any(
                env,
                DEFAULT_GCP_BILLING_TABLE,
                "COST_INSIGHT_GCP_BILLING_TABLE",
                "COST_GCP_BILLING_TABLE",
            ),
            account_id=_read_any(
                env,
                DEFAULT_GCP_ACCOUNT_ID,
                "COST_INSIGHT_GCP_ACCOUNT_ID",
                "COST_GCP_ACCOUNT_ID",
            ),
            earliest_usage_date=_read_date_any(
                env,
                ("COST_INSIGHT_EARLIEST_USAGE_DATE", "COST_EARLIEST_USAGE_DATE"),
                DEFAULT_EARLIEST_USAGE_DATE,
            ),
            sync_overlap_days=_read_int_any(
                env,
                ("COST_INSIGHT_SYNC_OVERLAP_DAYS", "COST_SYNC_OVERLAP_DAYS"),
                3,
            ),
            sync_lag_days=_read_int_any(
                env,
                ("COST_INSIGHT_SYNC_LAG_DAYS", "COST_SYNC_LAG_DAYS"),
                5,
            ),
            export_overlap_days=_read_non_negative_int_any(
                env,
                ("COST_INSIGHT_EXPORT_OVERLAP_DAYS", "COST_EXPORT_OVERLAP_DAYS"),
                0,
            ),
            sync_initial_lookback_days=_read_optional_positive_int_any(
                env,
                (
                    "COST_INSIGHT_SYNC_INITIAL_LOOKBACK_DAYS",
                    "COST_SYNC_INITIAL_LOOKBACK_DAYS",
                ),
            ),
            unmatched_resource_lag_days=_read_int_any(
                env,
                (
                    "COST_INSIGHT_UNMATCHED_RESOURCE_LAG_DAYS",
                    "COST_UNMATCHED_RESOURCE_LAG_DAYS",
                ),
                5,
            ),
            page_size=_read_int_any(
                env,
                ("COST_INSIGHT_SYNC_PAGE_SIZE", "COST_SYNC_PAGE_SIZE"),
                5000,
            ),
        ),
        aws_billing=AwsBillingSettings(
            billing_table=_read_any(
                env,
                DEFAULT_AWS_BILLING_TABLE,
                "COST_INSIGHT_AWS_BILLING_TABLE",
                "COST_AWS_BILLING_TABLE",
            ),
            account_id=_read_optional_any(
                env,
                "COST_INSIGHT_AWS_ACCOUNT_ID",
                "COST_AWS_ACCOUNT_ID",
            ),
            earliest_usage_date=_read_date_any(
                env,
                ("COST_INSIGHT_AWS_EARLIEST_USAGE_DATE", "COST_AWS_EARLIEST_USAGE_DATE"),
                DEFAULT_EARLIEST_USAGE_DATE,
            ),
            export_overlap_months=_read_non_negative_int_any(
                env,
                (
                    "COST_INSIGHT_AWS_EXPORT_OVERLAP_MONTHS",
                    "COST_AWS_EXPORT_OVERLAP_MONTHS",
                ),
                1,
            ),
            sync_initial_lookback_months=_read_optional_positive_int_any(
                env,
                (
                    "COST_INSIGHT_AWS_SYNC_INITIAL_LOOKBACK_MONTHS",
                    "COST_AWS_SYNC_INITIAL_LOOKBACK_MONTHS",
                ),
            ),
            page_size=_read_int_any(
                env,
                ("COST_INSIGHT_AWS_SYNC_PAGE_SIZE", "COST_AWS_SYNC_PAGE_SIZE"),
                5000,
            ),
        ),
        log_level=_read_any(env, "INFO", "COST_INSIGHT_LOG_LEVEL", "COST_LOG_LEVEL").upper(),
    )


@lru_cache(maxsize=1)
def get_settings(require_database: bool = True) -> Settings:
    return load_settings(require_database=require_database)


def _load_database_settings(
    environ: Mapping[str, str],
    *,
    require_database: bool,
) -> DatabaseSettings:
    database_url = (
        _read_any(environ, "", "COST_INSIGHT_DB_URL", "COST_DB_URL", "CI_DASHBOARD_DB_URL")
        or None
    )
    ssl_ca = _read_any(environ, "", "COST_INSIGHT_TIDB_SSL_CA", "COST_TIDB_SSL_CA", "TIDB_SSL_CA")
    ssl_ca = ssl_ca or None
    if database_url:
        return DatabaseSettings(
            url=database_url,
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=ssl_ca,
        )
    if not require_database:
        return DatabaseSettings(
            url=None,
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=ssl_ca,
        )
    return DatabaseSettings(
        url=None,
        host=_read_required_any(environ, "COST_INSIGHT_TIDB_HOST", "COST_TIDB_HOST", "TIDB_HOST"),
        port=_read_int_any(
            environ,
            ("COST_INSIGHT_TIDB_PORT", "COST_TIDB_PORT", "TIDB_PORT"),
            4000,
        ),
        user=_read_required_any(environ, "COST_INSIGHT_TIDB_USER", "COST_TIDB_USER", "TIDB_USER"),
        password=_read_required_any(
            environ,
            "COST_INSIGHT_TIDB_PASSWORD",
            "COST_TIDB_PASSWORD",
            "TIDB_PASSWORD",
        ),
        database=_read_required_any(environ, "COST_INSIGHT_TIDB_DB", "COST_TIDB_DB", "TIDB_DB"),
        ssl_ca=ssl_ca,
    )


def _read_any(environ: Mapping[str, str], default: str, *keys: str) -> str:
    for key in keys:
        value = environ.get(key)
        if value is not None and value.strip() != "":
            return value
    return default


def _read_required_any(environ: Mapping[str, str], *keys: str) -> str:
    for key in keys:
        value = environ.get(key)
        if value is not None and value.strip() != "":
            return value
    raise ValueError(f"Missing required environment variable: {' or '.join(keys)}")


def _read_optional_any(environ: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = environ.get(key)
        if value is not None and value.strip() != "":
            return value
    return None


def _read_int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{key} must be positive, got {raw!r}")
    return value


def _read_int_any(environ: Mapping[str, str], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        if key in environ:
            return _read_int(environ, key, default)
    return default


def _read_non_negative_int(
    environ: Mapping[str, str],
    key: str,
    default: int,
) -> int:
    raw = environ.get(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{key} must be non-negative, got {raw!r}")
    return value


def _read_non_negative_int_any(
    environ: Mapping[str, str],
    keys: tuple[str, ...],
    default: int,
) -> int:
    for key in keys:
        if key in environ:
            return _read_non_negative_int(environ, key, default)
    return default


def _read_optional_positive_int_any(
    environ: Mapping[str, str],
    keys: tuple[str, ...],
) -> int | None:
    for key in keys:
        raw = environ.get(key)
        if raw is None or raw.strip() == "":
            continue
        return _read_int(environ, key, 1)
    return None


def _read_date_any(
    environ: Mapping[str, str],
    keys: tuple[str, ...],
    default: date,
) -> date:
    for key in keys:
        raw = environ.get(key)
        if raw is None or raw.strip() == "":
            continue
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be an ISO date, got {raw!r}") from exc
    return default
