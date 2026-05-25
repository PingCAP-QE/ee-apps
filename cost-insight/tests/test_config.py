import pytest

from datetime import date

from cost_insight.common.config import DEFAULT_GCP_ACCOUNT_ID, DEFAULT_GCP_BILLING_TABLE, load_settings


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
