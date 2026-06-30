import pytest

from datetime import date

from cost_insight.common.config import (
    DEFAULT_AWS_BILLING_TABLE,
    DEFAULT_GCP_ACCOUNT_ID,
    DEFAULT_GCP_BILLING_TABLE,
    load_settings,
)


def test_load_settings_uses_cost_database_url() -> None:
    settings = load_settings(
        {
            "COST_INSIGHT_DB_URL": "mysql+pymysql://user:pass@127.0.0.1:4000/cost_insight",
            "COST_INSIGHT_GCP_ACCOUNT_ID": "custom-project",
            "COST_INSIGHT_SYNC_OVERLAP_DAYS": "5",
            "COST_INSIGHT_SYNC_LAG_DAYS": "7",
            "COST_INSIGHT_EXPORT_OVERLAP_DAYS": "1",
            "COST_INSIGHT_SYNC_INITIAL_LOOKBACK_DAYS": "30",
            "COST_INSIGHT_UNMATCHED_RESOURCE_LAG_DAYS": "6",
            "COST_INSIGHT_EARLIEST_USAGE_DATE": "2026-02-01",
        }
    )

    assert settings.database.url == "mysql+pymysql://user:pass@127.0.0.1:4000/cost_insight"
    assert settings.gcp_billing.account_id == "custom-project"
    assert settings.gcp_billing.sync_overlap_days == 5
    assert settings.gcp_billing.sync_lag_days == 7
    assert settings.gcp_billing.export_overlap_days == 1
    assert settings.gcp_billing.sync_initial_lookback_days == 30
    assert settings.gcp_billing.unmatched_resource_lag_days == 6
    assert settings.gcp_billing.earliest_usage_date == date(2026, 2, 1)


def test_load_settings_can_skip_database_for_validation() -> None:
    settings = load_settings({}, require_database=False)

    assert settings.database.url is None
    assert settings.gcp_billing.account_id == DEFAULT_GCP_ACCOUNT_ID
    assert settings.gcp_billing.billing_table == DEFAULT_GCP_BILLING_TABLE
    assert settings.aws_billing.billing_table == DEFAULT_AWS_BILLING_TABLE
    assert settings.aws_billing.account_id is None
    assert settings.gcs_cache.ac_retention_days == 10
    assert settings.gcs_cache.cas_retention_days == 15
    assert settings.gcs_cache.cleanup_safety_buffer_days == 1
    assert settings.gcs_cache.last_seen_excluded_get_user_agent == "TransferService"
    assert settings.gcs_cache.last_seen_excluded_get_principal_email is None
    assert settings.gcs_cache.ac_reference_max_index_staleness_hours == 12


def test_load_settings_falls_back_to_tidb_parts() -> None:
    settings = load_settings(
        {
            "COST_TIDB_HOST": "127.0.0.1",
            "COST_TIDB_PORT": "4001",
            "COST_TIDB_USER": "user",
            "COST_TIDB_PASSWORD": "pass",
            "COST_TIDB_DB": "cost",
            "COST_TIDB_SSL_CA": "/tmp/ca.pem",
            "COST_GCP_BILLING_TABLE": "billing.table",
            "COST_SYNC_PAGE_SIZE": "2000",
            "COST_LOG_LEVEL": "debug",
        }
    )

    assert settings.database.host == "127.0.0.1"
    assert settings.database.port == 4001
    assert settings.database.ssl_ca == "/tmp/ca.pem"
    assert settings.gcp_billing.billing_table == "billing.table"
    assert settings.gcp_billing.page_size == 2000
    assert settings.log_level == "DEBUG"


def test_load_settings_reads_aws_billing_settings() -> None:
    settings = load_settings(
        {
            "COST_INSIGHT_DB_URL": "mysql+pymysql://user:pass@127.0.0.1:4000/cost_insight",
            "COST_INSIGHT_AWS_ACCOUNT_ID": "946646677266",
            "COST_INSIGHT_AWS_BILLING_TABLE": "aws.billing.table",
            "COST_INSIGHT_AWS_EARLIEST_USAGE_DATE": "2026-01-01",
            "COST_INSIGHT_AWS_EXPORT_OVERLAP_MONTHS": "2",
            "COST_INSIGHT_AWS_SYNC_INITIAL_LOOKBACK_MONTHS": "6",
            "COST_INSIGHT_AWS_SYNC_PAGE_SIZE": "1000",
        }
    )

    assert settings.aws_billing.account_id == "946646677266"
    assert settings.aws_billing.billing_table == "aws.billing.table"
    assert settings.aws_billing.earliest_usage_date == date(2026, 1, 1)
    assert settings.aws_billing.export_overlap_months == 2
    assert settings.aws_billing.sync_initial_lookback_months == 6
    assert settings.aws_billing.page_size == 1000


def test_load_settings_reads_gcs_cache_settings_without_database() -> None:
    settings = load_settings(
        {
            "GOOGLE_CLOUD_PROJECT": "override-project",
            "COST_INSIGHT_GCS_CACHE_BUCKET_NAME": "custom-bucket",
            "COST_INSIGHT_GCS_CACHE_DATASET": "custom_dataset",
            "COST_INSIGHT_GCS_CACHE_AUDIT_LOG_TABLE": "raw_table",
            "COST_INSIGHT_GCS_CACHE_LAST_SEEN_DAILY_TABLE": "daily_table",
            "COST_INSIGHT_GCS_CACHE_LAST_SEEN_CURRENT_TABLE": "current_table",
            "COST_INSIGHT_GCS_CACHE_LAST_SEEN_EXCLUDED_GET_USER_AGENT": "CustomTransferService",
            "COST_INSIGHT_GCS_CACHE_LAST_SEEN_EXCLUDED_GET_PRINCIPAL_EMAIL": "cleanup@example.com",
            "COST_INSIGHT_GCS_CACHE_AC_REFERENCE_MAX_INDEX_STALENESS_HOURS": "6",
            "COST_INSIGHT_GCS_CACHE_AC_RETENTION_DAYS": "21",
            "COST_INSIGHT_GCS_CACHE_CAS_RETENTION_DAYS": "35",
            "COST_INSIGHT_GCS_CACHE_SAFETY_BUFFER_DAYS": "2",
            "COST_INSIGHT_GCS_CACHE_CLEANUP_SAMPLE_LIMIT": "25",
            "COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_OBJECTS": "500",
            "COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_CAS_OBJECTS": "100",
            "COST_INSIGHT_GCS_CACHE_CLEANUP_AC_DELETE_BATCH_SIZE": "200",
            "COST_INSIGHT_GCS_CACHE_CLEANUP_BATCH_SIZE": "50",
            "COST_INSIGHT_GCS_CACHE_CLEANUP_MANIFEST_BUCKET": "manifest-bucket",
            "COST_INSIGHT_GCS_CACHE_CLEANUP_MANIFEST_PREFIX": "manifest-prefix",
            "COST_INSIGHT_GCS_CACHE_CLEANUP_CANDIDATE_TTL_DAYS": "9",
        },
        require_database=False,
    )

    assert settings.gcs_cache.project_id == "override-project"
    assert settings.gcs_cache.bucket_name == "custom-bucket"
    assert settings.gcs_cache.dataset == "custom_dataset"
    assert settings.gcs_cache.audit_log_table == "raw_table"
    assert settings.gcs_cache.last_seen_daily_table == "daily_table"
    assert settings.gcs_cache.last_seen_current_table == "current_table"
    assert settings.gcs_cache.last_seen_excluded_get_user_agent == "CustomTransferService"
    assert settings.gcs_cache.last_seen_excluded_get_principal_email == "cleanup@example.com"
    assert settings.gcs_cache.ac_reference_max_index_staleness_hours == 6
    assert settings.gcs_cache.ac_retention_days == 21
    assert settings.gcs_cache.cas_retention_days == 35
    assert settings.gcs_cache.cleanup_safety_buffer_days == 2
    assert settings.gcs_cache.cleanup_sample_limit == 25
    assert settings.gcs_cache.cleanup_max_delete_objects == 500
    assert settings.gcs_cache.cleanup_max_delete_cas_objects == 100
    assert settings.gcs_cache.cleanup_ac_delete_batch_size == 200
    assert settings.gcs_cache.cleanup_batch_size == 50
    assert settings.gcs_cache.cleanup_manifest_bucket == "manifest-bucket"
    assert settings.gcs_cache.cleanup_manifest_prefix == "manifest-prefix"
    assert settings.gcs_cache.cleanup_candidate_ttl_days == 9


def test_load_settings_requires_database_when_not_skipped() -> None:
    with pytest.raises(ValueError, match="Missing required environment variable"):
        load_settings({})


def test_load_settings_rejects_invalid_positive_int() -> None:
    with pytest.raises(ValueError, match="COST_INSIGHT_SYNC_PAGE_SIZE must be positive"):
        load_settings(
            {
                "COST_INSIGHT_DB_URL": "mysql+pymysql://user:pass@127.0.0.1:4000/cost",
                "COST_INSIGHT_SYNC_PAGE_SIZE": "0",
            }
        )


def test_load_settings_rejects_invalid_gcs_cache_positive_int() -> None:
    with pytest.raises(
        ValueError, match="COST_INSIGHT_GCS_CACHE_AC_RETENTION_DAYS must be positive"
    ):
        load_settings(
            {
                "COST_INSIGHT_DB_URL": "mysql+pymysql://user:pass@127.0.0.1:4000/cost",
                "COST_INSIGHT_GCS_CACHE_AC_RETENTION_DAYS": "-1",
            }
        )

    with pytest.raises(
        ValueError,
        match="COST_INSIGHT_GCS_CACHE_AC_REFERENCE_MAX_INDEX_STALENESS_HOURS must be positive",
    ):
        load_settings(
            {
                "COST_INSIGHT_DB_URL": "mysql+pymysql://user:pass@127.0.0.1:4000/cost",
                "COST_INSIGHT_GCS_CACHE_AC_REFERENCE_MAX_INDEX_STALENESS_HOURS": "0",
            }
        )


def test_load_settings_rejects_invalid_int() -> None:
    with pytest.raises(ValueError, match="COST_INSIGHT_SYNC_OVERLAP_DAYS must be an integer"):
        load_settings(
            {
                "COST_INSIGHT_DB_URL": "mysql+pymysql://user:pass@127.0.0.1:4000/cost",
                "COST_INSIGHT_SYNC_OVERLAP_DAYS": "abc",
            }
        )


def test_load_settings_rejects_invalid_cost_refine_values() -> None:
    with pytest.raises(ValueError, match="COST_INSIGHT_EXPORT_OVERLAP_DAYS must be non-negative"):
        load_settings(
            {
                "COST_INSIGHT_DB_URL": "mysql+pymysql://user:pass@127.0.0.1:4000/cost",
                "COST_INSIGHT_EXPORT_OVERLAP_DAYS": "-1",
            }
        )

    with pytest.raises(ValueError, match="COST_INSIGHT_EARLIEST_USAGE_DATE must be an ISO date"):
        load_settings(
            {
                "COST_INSIGHT_DB_URL": "mysql+pymysql://user:pass@127.0.0.1:4000/cost",
                "COST_INSIGHT_EARLIEST_USAGE_DATE": "not-a-date",
            }
        )
