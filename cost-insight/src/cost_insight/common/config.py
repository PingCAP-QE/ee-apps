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
DEFAULT_GCS_CACHE_BUCKET = "pingcap-ci-bazel-remote-cache-us-central1"
DEFAULT_GCS_CACHE_DATASET = "ci_bazel_cache_logs"
DEFAULT_GCS_CACHE_AUDIT_LOG_TABLE = "cloudaudit_googleapis_com_data_access"
DEFAULT_GCS_CACHE_LAST_SEEN_DAILY_TABLE = "gcs_cache_object_last_seen_daily"
DEFAULT_GCS_CACHE_LAST_SEEN_CURRENT_TABLE = "gcs_cache_object_last_seen_current"
DEFAULT_GCS_CACHE_LAST_SEEN_EXCLUDED_GET_USER_AGENT = "TransferService"
DEFAULT_GCS_CACHE_AC_CAS_REFERENCES_TABLE = "gcs_cache_ac_cas_references"
DEFAULT_GCS_CACHE_AC_REFERENCE_INDEX_STATE_TABLE = "gcs_cache_ac_reference_index_state"
DEFAULT_GCS_CACHE_CLEANUP_MANIFEST_BUCKET = "pingcap-ci-console-logs-us-central1"
DEFAULT_GCS_CACHE_CLEANUP_MANIFEST_PREFIX = "gcs-cache-steady-state-delete"


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
class GcsCacheSettings:
    project_id: str = DEFAULT_GCP_ACCOUNT_ID
    bucket_name: str = DEFAULT_GCS_CACHE_BUCKET
    dataset: str = DEFAULT_GCS_CACHE_DATASET
    audit_log_table: str = DEFAULT_GCS_CACHE_AUDIT_LOG_TABLE
    last_seen_daily_table: str = DEFAULT_GCS_CACHE_LAST_SEEN_DAILY_TABLE
    last_seen_current_table: str = DEFAULT_GCS_CACHE_LAST_SEEN_CURRENT_TABLE
    last_seen_excluded_get_user_agent: str = DEFAULT_GCS_CACHE_LAST_SEEN_EXCLUDED_GET_USER_AGENT
    last_seen_excluded_get_principal_email: str | None = None
    ac_cas_references_table: str = DEFAULT_GCS_CACHE_AC_CAS_REFERENCES_TABLE
    ac_reference_index_state_table: str = DEFAULT_GCS_CACHE_AC_REFERENCE_INDEX_STATE_TABLE
    ac_reference_shard_count: int = 256
    ac_reference_batch_size: int = 500
    ac_reference_download_workers: int = 64
    ac_retention_days: int = 14
    cas_retention_days: int = 21
    cleanup_safety_buffer_days: int = 1
    cleanup_sample_limit: int = 10
    cleanup_max_delete_objects: int = 10000000
    cleanup_batch_size: int = 1000
    cleanup_manifest_bucket: str = DEFAULT_GCS_CACHE_CLEANUP_MANIFEST_BUCKET
    cleanup_manifest_prefix: str = DEFAULT_GCS_CACHE_CLEANUP_MANIFEST_PREFIX
    cleanup_candidate_ttl_days: int = 7


@dataclass(frozen=True)
class Settings:
    database: DatabaseSettings
    gcp_billing: GcpBillingSettings = GcpBillingSettings()
    aws_billing: AwsBillingSettings = AwsBillingSettings()
    gcs_cache: GcsCacheSettings = GcsCacheSettings()
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
        gcs_cache=GcsCacheSettings(
            project_id=_read_any(
                env,
                DEFAULT_GCP_ACCOUNT_ID,
                "COST_INSIGHT_GCS_CACHE_PROJECT_ID",
                "COST_GCS_CACHE_PROJECT_ID",
                "GOOGLE_CLOUD_PROJECT",
            ),
            bucket_name=_read_any(
                env,
                DEFAULT_GCS_CACHE_BUCKET,
                "COST_INSIGHT_GCS_CACHE_BUCKET_NAME",
                "COST_GCS_CACHE_BUCKET_NAME",
            ),
            dataset=_read_any(
                env,
                DEFAULT_GCS_CACHE_DATASET,
                "COST_INSIGHT_GCS_CACHE_DATASET",
                "COST_GCS_CACHE_DATASET",
            ),
            audit_log_table=_read_any(
                env,
                DEFAULT_GCS_CACHE_AUDIT_LOG_TABLE,
                "COST_INSIGHT_GCS_CACHE_AUDIT_LOG_TABLE",
                "COST_GCS_CACHE_AUDIT_LOG_TABLE",
            ),
            last_seen_daily_table=_read_any(
                env,
                DEFAULT_GCS_CACHE_LAST_SEEN_DAILY_TABLE,
                "COST_INSIGHT_GCS_CACHE_LAST_SEEN_DAILY_TABLE",
                "COST_GCS_CACHE_LAST_SEEN_DAILY_TABLE",
            ),
            last_seen_current_table=_read_any(
                env,
                DEFAULT_GCS_CACHE_LAST_SEEN_CURRENT_TABLE,
                "COST_INSIGHT_GCS_CACHE_LAST_SEEN_CURRENT_TABLE",
                "COST_GCS_CACHE_LAST_SEEN_CURRENT_TABLE",
            ),
            last_seen_excluded_get_user_agent=_read_any(
                env,
                DEFAULT_GCS_CACHE_LAST_SEEN_EXCLUDED_GET_USER_AGENT,
                "COST_INSIGHT_GCS_CACHE_LAST_SEEN_EXCLUDED_GET_USER_AGENT",
                "COST_GCS_CACHE_LAST_SEEN_EXCLUDED_GET_USER_AGENT",
            ),
            last_seen_excluded_get_principal_email=_read_optional_any(
                env,
                "COST_INSIGHT_GCS_CACHE_LAST_SEEN_EXCLUDED_GET_PRINCIPAL_EMAIL",
                "COST_GCS_CACHE_LAST_SEEN_EXCLUDED_GET_PRINCIPAL_EMAIL",
            ),
            ac_cas_references_table=_read_any(
                env,
                DEFAULT_GCS_CACHE_AC_CAS_REFERENCES_TABLE,
                "COST_INSIGHT_GCS_CACHE_AC_CAS_REFERENCES_TABLE",
                "COST_GCS_CACHE_AC_CAS_REFERENCES_TABLE",
            ),
            ac_reference_index_state_table=_read_any(
                env,
                DEFAULT_GCS_CACHE_AC_REFERENCE_INDEX_STATE_TABLE,
                "COST_INSIGHT_GCS_CACHE_AC_REFERENCE_INDEX_STATE_TABLE",
                "COST_GCS_CACHE_AC_REFERENCE_INDEX_STATE_TABLE",
            ),
            ac_reference_shard_count=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_AC_REFERENCE_SHARD_COUNT",
                    "COST_GCS_CACHE_AC_REFERENCE_SHARD_COUNT",
                ),
                256,
            ),
            ac_reference_batch_size=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_AC_REFERENCE_BATCH_SIZE",
                    "COST_GCS_CACHE_AC_REFERENCE_BATCH_SIZE",
                ),
                500,
            ),
            ac_reference_download_workers=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_AC_REFERENCE_DOWNLOAD_WORKERS",
                    "COST_GCS_CACHE_AC_REFERENCE_DOWNLOAD_WORKERS",
                ),
                64,
            ),
            ac_retention_days=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_AC_RETENTION_DAYS",
                    "COST_GCS_CACHE_AC_RETENTION_DAYS",
                ),
                14,
            ),
            cas_retention_days=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_CAS_RETENTION_DAYS",
                    "COST_GCS_CACHE_CAS_RETENTION_DAYS",
                ),
                21,
            ),
            cleanup_safety_buffer_days=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_SAFETY_BUFFER_DAYS",
                    "COST_GCS_CACHE_SAFETY_BUFFER_DAYS",
                ),
                1,
            ),
            cleanup_sample_limit=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_CLEANUP_SAMPLE_LIMIT",
                    "COST_GCS_CACHE_CLEANUP_SAMPLE_LIMIT",
                ),
                10,
            ),
            cleanup_max_delete_objects=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_OBJECTS",
                    "COST_GCS_CACHE_CLEANUP_MAX_DELETE_OBJECTS",
                ),
                10000000,
            ),
            cleanup_batch_size=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_CLEANUP_BATCH_SIZE",
                    "COST_GCS_CACHE_CLEANUP_BATCH_SIZE",
                ),
                1000,
            ),
            cleanup_manifest_bucket=_read_any(
                env,
                DEFAULT_GCS_CACHE_CLEANUP_MANIFEST_BUCKET,
                "COST_INSIGHT_GCS_CACHE_CLEANUP_MANIFEST_BUCKET",
                "COST_GCS_CACHE_CLEANUP_MANIFEST_BUCKET",
            ),
            cleanup_manifest_prefix=_read_any(
                env,
                DEFAULT_GCS_CACHE_CLEANUP_MANIFEST_PREFIX,
                "COST_INSIGHT_GCS_CACHE_CLEANUP_MANIFEST_PREFIX",
                "COST_GCS_CACHE_CLEANUP_MANIFEST_PREFIX",
            ),
            cleanup_candidate_ttl_days=_read_positive_int_any(
                env,
                (
                    "COST_INSIGHT_GCS_CACHE_CLEANUP_CANDIDATE_TTL_DAYS",
                    "COST_GCS_CACHE_CLEANUP_CANDIDATE_TTL_DAYS",
                ),
                7,
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


def _read_positive_int_any(environ: Mapping[str, str], keys: tuple[str, ...], default: int) -> int:
    return _read_int_any(environ, keys, default)


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
