import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from ci_dashboard.common.config import (
    AwsEbsSettings,
    DatabaseSettings,
    GcpBlockVolumeSettings,
    JobSettings,
    Settings,
)
from ci_dashboard.jobs.state_store import get_job_state
from ci_dashboard.jobs import sync_unattached_ebs_volumes as sync_unattached_ebs_volumes_module
from ci_dashboard.jobs.sync_unattached_ebs_volumes import (
    UNATTACHED_BLOCK_VOLUME_SYNC_JOB,
    run_sync_unattached_block_volumes,
    run_sync_unattached_ebs_volumes,
)


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, *, Filters):
        assert Filters == [{"Name": "status", "Values": ["available"]}]
        return self._pages


class _FakeEc2Client:
    def __init__(self, pages):
        self._pages = pages

    def get_paginator(self, name):
        assert name == "describe_volumes"
        return _FakePaginator(self._pages)


def _settings() -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=None,
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=None,
        ),
        jobs=JobSettings(),
        aws_ebs=AwsEbsSettings(
            regions=("us-east-1",),
            account_id="123456789012",
            owner_tag_keys=("owner", "github"),
        ),
        gcp_block_volumes=GcpBlockVolumeSettings(
            projects=("gcp-project",),
            owner_label_keys=("owner", "user"),
        ),
    )


def test_sync_unattached_block_volumes_scans_aws_and_gcp_and_preserves_first_seen(
    sqlite_engine,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_unattached_block_volume_daily (
                  snapshot_date, vendor, account_id, region, availability_zone,
                  volume_id, state, size_gib, tags_json, owner, owner_source,
                  first_seen_available, source_created_at, observed_at
                ) VALUES (
                  '2026-07-20', 'aws', '123456789012', 'us-east-1', 'us-east-1a',
                  'vol-existing', 'available', 64, '{"owner":"old@example.com"}',
                  'old@example.com', 'tag:owner', '2026-07-01',
                  '2026-07-04 00:00:00', '2026-07-20 00:00:00'
                ), (
                  '2026-07-20', 'gcp', 'gcp-project', 'us-west1', 'us-west1-a',
                  'disk-existing', 'READY', 200, '{"owner":"old@example.com"}',
                  'old@example.com', 'label:owner', '2026-07-03',
                  '2026-07-02 00:00:00', '2026-07-20 00:00:00'
                ), (
                  '2025-07-17', 'aws', '123456789012', 'us-east-1', 'us-east-1a',
                  'vol-existing', 'available', 64, '{"owner":"cutoff@example.com"}',
                  'cutoff@example.com', 'tag:owner', '2025-07-10',
                  '2025-07-09 00:00:00', '2025-07-17 00:00:00'
                ), (
                  '2025-07-16', 'aws', '123456789012', 'us-east-1', 'us-east-1a',
                  'vol-existing', 'available', 64, '{"owner":"expired@example.com"}',
                  'expired@example.com', 'tag:owner', '2025-07-01',
                  '2025-07-01 00:00:00', '2025-07-16 00:00:00'
                ), (
                  '2025-07-01', 'aws', '123456789012', 'us-east-1', 'us-east-1a',
                  'vol-expired', 'available', 8, '{}',
                  NULL, NULL, '2025-07-01',
                  '2025-07-01 00:00:00', '2025-07-01 00:00:00'
                )
                """
            )
        )

    pages_by_region = {
        "us-east-1": [
            {
                "Volumes": [
                    {
                        "VolumeId": "vol-existing",
                        "State": "available",
                        "AvailabilityZone": "us-east-1a",
                        "CreateTime": datetime(2026, 7, 5, tzinfo=timezone.utc),
                        "Size": 128,
                        "Tags": [
                            {"Key": "owner", "Value": "alice@example.com"},
                            {"Key": "Name", "Value": "stale-cache"},
                        ],
                    },
                    {
                        "VolumeId": "vol-new",
                        "State": "available",
                        "AvailabilityZone": "us-east-1b",
                        "CreateTime": datetime(2026, 7, 18, tzinfo=timezone.utc),
                        "Size": 32,
                        "Tags": [{"Key": "github", "Value": "bob"}],
                    },
                    {
                        "VolumeId": "vol-attached",
                        "State": "in-use",
                        "AvailabilityZone": "us-east-1c",
                        "Size": 256,
                    },
                ],
            }
        ],
    }
    gcp_pages = {
        ("gcp-project", None): {
            "items": {
                "zones/us-west1-a": {
                    "disks": [
                        {
                            "name": "disk-existing",
                            "status": "READY",
                            "zone": (
                                "https://www.googleapis.com/compute/v1/projects/"
                                "gcp-project/zones/us-west1-a"
                            ),
                            "type": (
                                "https://www.googleapis.com/compute/v1/projects/"
                                "gcp-project/zones/us-west1-a/diskTypes/pd-ssd"
                            ),
                            "sizeGb": "200",
                            "creationTimestamp": "2026-07-09T10:00:00.000-07:00",
                            "labels": {"owner": "carol@example.com", "env": "dev"},
                            "users": [],
                        },
                        {
                            "name": "disk-attached",
                            "status": "READY",
                            "zone": (
                                "https://www.googleapis.com/compute/v1/projects/"
                                "gcp-project/zones/us-west1-a"
                            ),
                            "sizeGb": "50",
                            "users": [
                                "https://www.googleapis.com/compute/v1/projects/"
                                "gcp-project/zones/us-west1-a/instances/worker-1"
                            ],
                        },
                    ],
                },
                "regions/us-east1": {
                    "disks": [
                        {
                            "name": "regional-disk",
                            "status": "READY",
                            "region": (
                                "https://www.googleapis.com/compute/v1/projects/"
                                "gcp-project/regions/us-east1"
                            ),
                            "sizeGb": "500",
                            "creationTimestamp": "2026-07-15T08:30:00Z",
                            "labels": {"user": "dave"},
                        },
                    ]
                },
            }
        }
    }

    summary = run_sync_unattached_block_volumes(
        sqlite_engine,
        _settings(),
        snapshot_date=date(2026, 7, 22),
        ec2_client_factory=lambda region: _FakeEc2Client(pages_by_region[region]),
        gcp_disk_page_fetcher=lambda project, page_token: gcp_pages[(project, page_token)],
    )

    assert summary.snapshot_date == date(2026, 7, 22)
    assert summary.regions_scanned == 1
    assert summary.gcp_projects_scanned == 1
    assert summary.volumes_available == 4
    assert summary.rows_written == 4

    with sqlite_engine.begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT
                  vendor,
                  volume_id,
                  region,
                  availability_zone,
                  size_gib,
                  tags_json,
                  owner,
                  owner_source,
                  first_seen_available,
                  source_created_at
                FROM cost_unattached_block_volume_daily
                WHERE snapshot_date = '2026-07-22'
                ORDER BY vendor, volume_id
                """
            )
        ).mappings().all()
        state = get_job_state(connection, UNATTACHED_BLOCK_VOLUME_SYNC_JOB)
        expired_count = connection.execute(
            text(
                """
                SELECT COUNT(*) AS count
                FROM cost_unattached_block_volume_daily
                WHERE snapshot_date < '2025-07-17'
                """
            )
        ).scalar_one()
        cutoff_count = connection.execute(
            text(
                """
                SELECT COUNT(*) AS count
                FROM cost_unattached_block_volume_daily
                WHERE snapshot_date = '2025-07-17'
                  AND volume_id = 'vol-existing'
                """
            )
        ).scalar_one()

    assert [(row["vendor"], row["volume_id"]) for row in rows] == [
        ("aws", "vol-existing"),
        ("aws", "vol-new"),
        ("gcp", "disk-existing"),
        ("gcp", "regional-disk"),
    ]
    assert rows[0]["size_gib"] == 128
    assert json.loads(rows[0]["tags_json"]) == {
        "Name": "stale-cache",
        "owner": "alice@example.com",
    }
    assert rows[0]["owner"] == "alice@example.com"
    assert rows[0]["owner_source"] == "tag:owner"
    assert rows[0]["first_seen_available"] == "2025-07-10"
    assert str(rows[0]["source_created_at"]).startswith("2025-07-09")
    assert rows[1]["owner"] == "bob"
    assert rows[1]["owner_source"] == "tag:github"
    assert rows[1]["first_seen_available"] == "2026-07-22"
    assert str(rows[1]["source_created_at"]).startswith("2026-07-18")
    assert rows[2]["region"] == "us-west1"
    assert rows[2]["availability_zone"] == "us-west1-a"
    assert json.loads(rows[2]["tags_json"]) == {
        "disk_type": "pd-ssd",
        "env": "dev",
        "owner": "carol@example.com",
    }
    assert rows[2]["owner"] == "carol@example.com"
    assert rows[2]["owner_source"] == "label:owner"
    assert rows[2]["first_seen_available"] == "2026-07-03"
    assert str(rows[2]["source_created_at"]).startswith("2026-07-02")
    assert rows[3]["region"] == "us-east1"
    assert rows[3]["availability_zone"] is None
    assert rows[3]["owner"] == "dave"
    assert rows[3]["owner_source"] == "label:user"
    assert rows[3]["first_seen_available"] == "2026-07-22"
    assert str(rows[3]["source_created_at"]).startswith("2026-07-15")
    assert state is not None
    assert state.last_status == "succeeded"
    assert state.watermark["volumes_available"] == 4
    assert expired_count == 0
    assert cutoff_count == 1


def test_delete_expired_snapshots_commits_each_batch(sqlite_engine, monkeypatch) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_unattached_block_volume_daily (
                  snapshot_date, vendor, account_id, region, availability_zone,
                  volume_id, state, size_gib, tags_json, owner, owner_source,
                  first_seen_available, source_created_at, observed_at
                ) VALUES (
                  '2025-07-15', 'aws', '123456789012', 'us-east-1', 'us-east-1a',
                  'vol-expired-1', 'available', 64, '{}',
                  NULL, NULL, '2025-07-15',
                  '2025-07-15 00:00:00', '2025-07-15 00:00:00'
                ), (
                  '2025-07-15', 'aws', '123456789012', 'us-east-1', 'us-east-1b',
                  'vol-expired-2', 'available', 64, '{}',
                  NULL, NULL, '2025-07-15',
                  '2025-07-15 00:00:00', '2025-07-15 00:00:00'
                ), (
                  '2025-07-17', 'aws', '123456789012', 'us-east-1', 'us-east-1c',
                  'vol-retained', 'available', 64, '{}',
                  NULL, NULL, '2025-07-17',
                  '2025-07-17 00:00:00', '2025-07-17 00:00:00'
                )
                """
            )
        )

    original_delete_batch = sync_unattached_ebs_volumes_module._delete_expired_snapshot_batch
    calls = 0

    def fail_second_batch(connection, *, cutoff_date):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("delete failed")
        return original_delete_batch(connection, cutoff_date=cutoff_date)

    monkeypatch.setattr(
        sync_unattached_ebs_volumes_module,
        "UNATTACHED_BLOCK_VOLUME_DELETE_BATCH_SIZE",
        1,
    )
    monkeypatch.setattr(
        sync_unattached_ebs_volumes_module,
        "_delete_expired_snapshot_batch",
        fail_second_batch,
    )

    with pytest.raises(RuntimeError, match="delete failed"):
        sync_unattached_ebs_volumes_module._delete_expired_snapshots(
            sqlite_engine,
            snapshot_date=date(2026, 7, 22),
        )

    with sqlite_engine.begin() as connection:
        expired_count = connection.execute(
            text(
                """
                SELECT COUNT(*) AS count
                FROM cost_unattached_block_volume_daily
                WHERE snapshot_date < '2025-07-17'
                """
            )
        ).scalar_one()
        retained_count = connection.execute(
            text(
                """
                SELECT COUNT(*) AS count
                FROM cost_unattached_block_volume_daily
                WHERE volume_id = 'vol-retained'
                """
            )
        ).scalar_one()

    assert expired_count == 1
    assert retained_count == 1


def test_sync_unattached_ebs_volumes_keeps_aws_only_alias(sqlite_engine) -> None:
    settings = _settings()
    summary = run_sync_unattached_ebs_volumes(
        sqlite_engine,
        settings,
        snapshot_date=date(2026, 7, 22),
        ec2_client_factory=lambda region: _FakeEc2Client({"us-east-1": []}[region]),
    )

    assert summary.regions_scanned == 1
    assert summary.gcp_projects_scanned == 0
