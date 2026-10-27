"""Offline rendered-text contracts for the flaky issue sync CronJob renderer."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = "render_flaky_issue_sync_cronjob.sh"
DEFAULT_BROKER_URL = "http://github-token-broker.github-token-broker.svc"


def successful_render(*args: str) -> str:
    result = subprocess.run(
        ["bash", str(PROJECT / "scripts" / SCRIPT), *args],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout


def test_default_render_uses_broker_and_service_account() -> None:
    manifest = successful_render()

    assert "          serviceAccountName: ci-dashboard\n" in manifest
    assert f'                - name: CI_DASHBOARD_GITHUB_BROKER_URL\n                  value: "{DEFAULT_BROKER_URL}"\n' in manifest
    assert "GITHUB_TOKEN" not in manifest
    assert "prow-github" not in manifest
    assert "CI_DASHBOARD_GITHUB_PAT_FALLBACK" not in manifest


def test_broker_url_override_is_rendered() -> None:
    manifest = successful_render("--github-broker-url", "http://broker.example.svc")

    assert 'value: "http://broker.example.svc"' in manifest


def test_pat_secret_renders_explicit_fallback() -> None:
    manifest = successful_render("--github-secret", "custom-github-pat")

    assert "                - name: GITHUB_TOKEN\n" in manifest
    assert "                      name: custom-github-pat\n" in manifest
    assert '                - name: CI_DASHBOARD_GITHUB_PAT_FALLBACK\n                  value: "true"\n' in manifest


def test_service_account_can_be_disabled() -> None:
    manifest = successful_render("--service-account", "")

    assert "serviceAccountName" not in manifest


@pytest.mark.parametrize(
    "args",
    [
        ["--unknown-flag"],
    ],
)
def test_invalid_arguments_are_rejected(args: list[str]) -> None:
    result = subprocess.run(
        ["bash", str(PROJECT / "scripts" / SCRIPT), *args],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip()
