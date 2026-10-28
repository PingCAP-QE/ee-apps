"""Configuration and renderer contracts for opt-in warehouse freshness checks."""
from pathlib import Path
import subprocess

import pytest

from ci_dashboard.common.config import load_settings

BASE = {"CI_DASHBOARD_DB_URL": "sqlite+pysqlite:///:memory:"}
PROJECT = "warehouse-f80fbbe8e8"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render_data_freshness_check_cronjob.sh"


def test_disabled_by_default_without_bigquery_credentials():
    settings = load_settings(BASE).cost_freshness
    assert not settings.enabled
    assert settings.project is None
    assert settings.lookback_days == 14
    assert settings.maximum_bytes_billed == 2 * 1024**3


def test_explicit_project_location_budget_and_exclusions():
    cfg = load_settings({**BASE, "FRESHNESS_COST_BQ_ENABLED": "true",
        "FRESHNESS_COST_BQ_PROJECT": PROJECT, "FRESHNESS_COST_BQ_LOCATION": "us-west1",
        "FRESHNESS_COST_BQ_MAXIMUM_BYTES_BILLED": "12345", "FRESHNESS_COST_BQ_TIMEOUT_SECONDS": "12",
        "FRESHNESS_COST_BQ_EXCLUDED_SOURCES": "azure/inactive-subscription, gcp/project-a"}).cost_freshness
    assert cfg.enabled and cfg.project == PROJECT and cfg.location == "us-west1"
    assert cfg.maximum_bytes_billed == 12345 and cfg.timeout_seconds == 12
    assert cfg.excluded_sources == ("azure/inactive-subscription", "gcp/project-a")


@pytest.mark.parametrize("key,value", [
    ("FRESHNESS_COST_BQ_ENABLED", "invalid"),
    ("FRESHNESS_COST_BQ_PROJECT", ""),
    ("FRESHNESS_COST_BQ_PROJECT", "project` UNION SELECT 1"),
    ("FRESHNESS_COST_BQ_LOCATION", ""),
    ("FRESHNESS_COST_BQ_LOOKBACK_DAYS", "7"),
    ("FRESHNESS_COST_BQ_LOOKBACK_DAYS", "63"),
    ("FRESHNESS_COST_BQ_LOOKBACK_DAYS", "not-an-int"),
    ("FRESHNESS_COST_BQ_MAXIMUM_BYTES_BILLED", "0"),
    ("FRESHNESS_COST_BQ_TIMEOUT_SECONDS", "-1"),
    ("FRESHNESS_COST_BQ_EXCLUDED_SOURCES", "missing-account"),
    ("FRESHNESS_COST_BQ_EXCLUDED_SOURCES", "gcp/a/b"),
])
def test_invalid_settings_fail_fast(key, value):
    with pytest.raises(ValueError):
        load_settings({**BASE, "FRESHNESS_COST_BQ_ENABLED": "true",
            "FRESHNESS_COST_BQ_PROJECT": PROJECT, key: value})


def render(*args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          check=False, timeout=10)


def test_default_renderer_does_not_enable_cloud_queries():
    result = render()
    assert result.returncode == 0, result.stderr
    assert "FRESHNESS_COST_BQ_ENABLED" not in result.stdout


def test_renderer_enables_only_explicit_project_and_workload_identity():
    result = render("--cost-bq-project", PROJECT, "--service-account", "cost-insight-gcp-billing",
                    "--cost-bq-excluded-sources", "azure/inactive-subscription")
    assert result.returncode == 0, result.stderr
    assert 'serviceAccountName: "cost-insight-gcp-billing"' in result.stdout
    assert 'name: FRESHNESS_COST_BQ_ENABLED\n                  value: "true"' in result.stdout
    assert f'name: FRESHNESS_COST_BQ_PROJECT\n                  value: "{PROJECT}"' in result.stdout
    assert 'name: FRESHNESS_COST_BQ_LOCATION\n                  value: "us-west1"' in result.stdout
    assert "IMPERSONATE" not in result.stdout


@pytest.mark.parametrize("args", [
    ["--cost-bq-project", PROJECT],
    ["--cost-bq-project", "", "--service-account", "cost-insight-gcp-billing"],
    ["--cost-bq-project", "invalid project", "--service-account", "billing"],
    ["--cost-bq-project", PROJECT, "--service-account", "bad\naccount"],
    ["--cost-bq-project", PROJECT, "--service-account", "billing", "--cost-bq-location", "us-west1\n"],
    ["--cost-bq-project", PROJECT, "--service-account", "billing", "--cost-bq-excluded-sources", 'gcp/a"\n'],
])
def test_renderer_rejects_implicit_identity_and_invalid_values(args):
    result = render(*args)
    assert result.returncode != 0 and not result.stdout
