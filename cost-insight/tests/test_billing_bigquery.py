from unittest.mock import Mock

import google.auth
import pytest
from google.auth import impersonated_credentials
from google.cloud import bigquery

from cost_insight.common.bigquery import build_billing_bigquery_client
from cost_insight.common.config import BillingBigQuerySettings, load_settings


def test_billing_client_preserves_adc_defaults(monkeypatch) -> None:
    client = Mock()
    monkeypatch.setattr(bigquery, "Client", client)
    auth = Mock(side_effect=AssertionError("must not impersonate by default"))
    monkeypatch.setattr(google.auth, "default", auth)

    assert build_billing_bigquery_client(BillingBigQuerySettings()) is client.return_value
    client.assert_called_once_with()
    auth.assert_not_called()


def test_billing_client_reads_project_location_and_sa_without_database(monkeypatch) -> None:
    monkeypatch.setenv("COST_INSIGHT_BILLING_BQ_PROJECT", "warehouse-f80fbbe8e8")
    monkeypatch.setenv("COST_INSIGHT_BILLING_BQ_LOCATION", "us-west1")
    monkeypatch.setenv("COST_INSIGHT_BILLING_BQ_IMPERSONATE_SERVICE_ACCOUNT", "reader@example.com")
    source_credentials = object()
    auth = Mock(return_value=(source_credentials, "old-project"))
    impersonate = Mock()
    client = Mock()
    monkeypatch.setattr(google.auth, "default", auth)
    monkeypatch.setattr(impersonated_credentials, "Credentials", impersonate)
    monkeypatch.setattr(bigquery, "Client", client)

    result = build_billing_bigquery_client()

    assert result is client.return_value
    auth.assert_called_once_with(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    impersonate.assert_called_once_with(
        source_credentials=source_credentials,
        target_principal="reader@example.com",
        target_scopes=["https://www.googleapis.com/auth/bigquery"],
        lifetime=900,
        quota_project_id="warehouse-f80fbbe8e8",
    )
    client.assert_called_once_with(
        project="warehouse-f80fbbe8e8",
        location="us-west1",
        credentials=impersonate.return_value,
    )


def test_billing_client_can_set_project_and_location_without_impersonation(monkeypatch) -> None:
    client = Mock()
    monkeypatch.setattr(bigquery, "Client", client)

    build_billing_bigquery_client(
        BillingBigQuerySettings(project="warehouse", location="us-west1")
    )

    client.assert_called_once_with(project="warehouse", location="us-west1")


def test_billing_client_does_not_fall_back_after_auth_failure(monkeypatch) -> None:
    monkeypatch.setattr(google.auth, "default", Mock(side_effect=ValueError("expired ADC")))
    client = Mock()
    monkeypatch.setattr(bigquery, "Client", client)

    with pytest.raises(ValueError, match="expired ADC"):
        build_billing_bigquery_client(
            BillingBigQuerySettings(impersonate_service_account="reader@example.com")
        )

    client.assert_not_called()


def test_billing_settings_do_not_change_cache_project() -> None:
    settings = load_settings(
        {
            "GOOGLE_CLOUD_PROJECT": "cache-project",
            "COST_INSIGHT_BILLING_BQ_PROJECT": "warehouse-f80fbbe8e8",
            "COST_INSIGHT_BILLING_BQ_LOCATION": "us-west1",
            "COST_INSIGHT_BILLING_BQ_IMPERSONATE_SERVICE_ACCOUNT": "reader@example.com",
        },
        require_database=False,
    )

    assert settings.billing_bigquery == BillingBigQuerySettings(
        project="warehouse-f80fbbe8e8",
        location="us-west1",
        impersonate_service_account="reader@example.com",
    )
    assert settings.gcs_cache.project_id == "cache-project"
    assert load_settings({}, require_database=False).billing_bigquery == BillingBigQuerySettings()
