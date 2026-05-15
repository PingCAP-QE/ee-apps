from __future__ import annotations

import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_jobs_dockerfile_uses_roster_cli_entrypoint() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile.jobs").read_text()

    assert "FROM python:3.12-slim" in dockerfile
    assert "COPY pyproject.toml README.md ./" in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "roster.jobs.cli"]' in dockerfile


def test_skaffold_builds_roster_jobs_image() -> None:
    skaffold = (PROJECT_ROOT / "skaffold.yaml").read_text()

    assert "apiVersion: skaffold/v4beta6" in skaffold
    assert "name: roster" in skaffold
    assert "image: roster-jobs" in skaffold
    assert "dockerfile: Dockerfile.jobs" in skaffold


def test_render_roster_sync_cronjob_outputs_expected_manifest() -> None:
    script = PROJECT_ROOT / "scripts" / "render_roster_sync_cronjob.sh"

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--namespace",
            "apps",
            "--image",
            "example/roster:dev",
            "--db-secret",
            "ci-dashboard-eq-prd-insight-db",
            "--lark-secret",
            "roster-lark",
            "--ca-secret",
            "roster-ca",
            "--service-account",
            "roster",
            "--suspend",
            "true",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    manifest = result.stdout
    assert "kind: CronJob" in manifest
    assert "name: roster-sync" in manifest
    assert "namespace: apps" in manifest
    assert "image: example/roster:dev" in manifest
    assert "serviceAccountName: roster" in manifest
    assert "suspend: true" in manifest
    assert "concurrencyPolicy: Forbid" in manifest
    assert "app.kubernetes.io/name: roster-sync" in manifest
    assert "app.kubernetes.io/part-of: roster" in manifest
    assert "name: ci-dashboard-eq-prd-insight-db" in manifest
    assert "name: roster-lark" in manifest
    assert "ROSTER_LOG_LEVEL" in manifest
    assert "ROSTER_TIDB_SSL_CA" in manifest
    assert "value: /var/run/roster/ssl/ca.crt" in manifest
    assert "secretName: roster-ca" in manifest
    assert "- sync-roster" in manifest
