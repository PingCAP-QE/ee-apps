from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import json
import os
import time
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.api.queries.ebs import (
    UNATTACHED_BLOCK_VOLUME_SYNC_JOB,
    UNATTACHED_EBS_SYNC_JOB,
)
from ci_dashboard.common.config import Settings
from ci_dashboard.jobs.state_store import mark_job_failed, mark_job_started, mark_job_succeeded

COMPUTE_DISKS_API_TEMPLATE = (
    "https://compute.googleapis.com/compute/v1/projects/{project}/aggregated/disks"
)
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
API_RETRY_ATTEMPTS = 3
API_RETRY_BASE_DELAY_SECONDS = 0.5
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
UNATTACHED_BLOCK_VOLUME_RETENTION_DAYS = 370
UNATTACHED_BLOCK_VOLUME_DELETE_BATCH_SIZE = 1000

GcpDiskPageFetcher = Callable[[str, str | None], Mapping[str, Any]]


@dataclass(frozen=True)
class UnattachedBlockVolumeSnapshot:
    snapshot_date: date
    vendor: str
    account_id: str
    region: str
    availability_zone: str | None
    volume_id: str
    state: str
    size_gib: float | None
    tags: dict[str, str]
    owner: str | None
    owner_source: str | None
    first_seen_available: date
    source_created_at: datetime | None
    observed_at: datetime


@dataclass(frozen=True)
class SyncUnattachedBlockVolumesSummary:
    snapshot_date: date
    regions_scanned: int
    gcp_projects_scanned: int
    volumes_available: int
    rows_written: int


@dataclass(frozen=True)
class ExistingBlockVolumeDates:
    first_seen_available: date
    source_created_at: datetime | None


SyncUnattachedEbsVolumesSummary = SyncUnattachedBlockVolumesSummary
UnattachedEbsVolumeSnapshot = UnattachedBlockVolumeSnapshot


def run_sync_unattached_block_volumes(
    engine: Engine,
    settings: Settings,
    *,
    snapshot_date: date | None = None,
    aws_regions: tuple[str, ...] | None = None,
    aws_account_id: str | None = None,
    ec2_client_factory: Callable[[str], Any] | None = None,
    gcp_projects: tuple[str, ...] | None = None,
    gcp_disk_page_fetcher: GcpDiskPageFetcher | None = None,
    google_access_token: str | None = None,
) -> SyncUnattachedBlockVolumesSummary:
    return _run_sync_unattached_block_volumes(
        engine,
        settings,
        job_name=UNATTACHED_BLOCK_VOLUME_SYNC_JOB,
        snapshot_date=snapshot_date,
        aws_regions=aws_regions,
        aws_account_id=aws_account_id,
        ec2_client_factory=ec2_client_factory,
        gcp_projects=gcp_projects,
        gcp_disk_page_fetcher=gcp_disk_page_fetcher,
        google_access_token=google_access_token,
    )


def run_sync_unattached_ebs_volumes(
    engine: Engine,
    settings: Settings,
    *,
    snapshot_date: date | None = None,
    regions: tuple[str, ...] | None = None,
    account_id: str | None = None,
    ec2_client_factory: Callable[[str], Any] | None = None,
) -> SyncUnattachedEbsVolumesSummary:
    resolved_regions = regions if regions is not None else settings.aws_ebs.regions
    if not resolved_regions:
        raise ValueError("CI_DASHBOARD_AWS_EBS_REGIONS must list at least one AWS region")

    return _run_sync_unattached_block_volumes(
        engine,
        settings,
        job_name=UNATTACHED_EBS_SYNC_JOB,
        snapshot_date=snapshot_date,
        aws_regions=resolved_regions,
        aws_account_id=account_id,
        ec2_client_factory=ec2_client_factory,
        gcp_projects=(),
    )


def _run_sync_unattached_block_volumes(
    engine: Engine,
    settings: Settings,
    *,
    job_name: str,
    snapshot_date: date | None,
    aws_regions: tuple[str, ...] | None,
    aws_account_id: str | None,
    ec2_client_factory: Callable[[str], Any] | None,
    gcp_projects: tuple[str, ...] | None,
    gcp_disk_page_fetcher: GcpDiskPageFetcher | None = None,
    google_access_token: str | None = None,
) -> SyncUnattachedBlockVolumesSummary:
    resolved_snapshot_date = snapshot_date or date.today()
    resolved_aws_regions = aws_regions if aws_regions is not None else settings.aws_ebs.regions
    resolved_gcp_projects = (
        gcp_projects if gcp_projects is not None else settings.gcp_block_volumes.projects
    )
    if not resolved_aws_regions and not resolved_gcp_projects:
        raise ValueError(
            "CI_DASHBOARD_AWS_EBS_REGIONS or CI_DASHBOARD_GCP_BLOCK_VOLUME_PROJECTS "
            "must list at least one scan target"
        )

    resolved_aws_account_id = (
        aws_account_id or settings.aws_ebs.account_id or _resolve_aws_account_id()
        if resolved_aws_regions
        else None
    )
    watermark = {
        "snapshot_date": resolved_snapshot_date.isoformat(),
        "aws_account_id": resolved_aws_account_id,
        "aws_regions": list(resolved_aws_regions),
        "gcp_projects": list(resolved_gcp_projects),
    }
    snapshots: list[UnattachedBlockVolumeSnapshot] = []

    with engine.begin() as connection:
        mark_job_started(connection, job_name, watermark)

    try:
        if resolved_aws_regions and resolved_aws_account_id:
            snapshots.extend(
                _scan_available_ebs_volumes(
                    account_id=resolved_aws_account_id,
                    regions=resolved_aws_regions,
                    snapshot_date=resolved_snapshot_date,
                    owner_tag_keys=settings.aws_ebs.owner_tag_keys,
                    ec2_client_factory=ec2_client_factory,
                )
            )
        if resolved_gcp_projects:
            snapshots.extend(
                _scan_unattached_gcp_disks(
                    projects=resolved_gcp_projects,
                    snapshot_date=resolved_snapshot_date,
                    owner_label_keys=settings.gcp_block_volumes.owner_label_keys,
                    disk_page_fetcher=gcp_disk_page_fetcher,
                    google_access_token=google_access_token,
                )
            )

        with engine.begin() as connection:
            existing_dates = _load_existing_volume_dates(
                connection,
                snapshot_date=resolved_snapshot_date,
            )
            snapshots = [
                _with_existing_volume_dates(snapshot, existing_dates)
                for snapshot in snapshots
            ]
            _upsert_snapshots(connection, snapshots)

        _delete_expired_snapshots(engine, snapshot_date=resolved_snapshot_date)

        with engine.begin() as connection:
            mark_job_succeeded(
                connection,
                job_name,
                {
                    **watermark,
                    "volumes_available": len(snapshots),
                },
            )
    except Exception as exc:
        with engine.begin() as connection:
            mark_job_failed(connection, job_name, watermark, str(exc))
        raise

    return SyncUnattachedBlockVolumesSummary(
        snapshot_date=resolved_snapshot_date,
        regions_scanned=len(resolved_aws_regions),
        gcp_projects_scanned=len(resolved_gcp_projects),
        volumes_available=len(snapshots),
        rows_written=len(snapshots),
    )


def _scan_available_ebs_volumes(
    *,
    account_id: str,
    regions: tuple[str, ...],
    snapshot_date: date,
    owner_tag_keys: tuple[str, ...],
    ec2_client_factory: Callable[[str], Any] | None,
) -> list[UnattachedBlockVolumeSnapshot]:
    observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    snapshots: list[UnattachedBlockVolumeSnapshot] = []
    for region in regions:
        client = (
            _build_ec2_client(region)
            if ec2_client_factory is None
            else ec2_client_factory(region)
        )
        paginator = client.get_paginator("describe_volumes")
        for page in paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}]):
            for volume in page.get("Volumes", []):
                state = str(volume.get("State") or "")
                if state != "available":
                    continue
                volume_id = str(volume.get("VolumeId") or "").strip()
                if not volume_id:
                    continue
                tags = _aws_tags_to_dict(volume.get("Tags"))
                owner, owner_source = _owner_from_tags(tags, owner_tag_keys, prefix="tag")
                snapshots.append(
                    UnattachedBlockVolumeSnapshot(
                        snapshot_date=snapshot_date,
                        vendor="aws",
                        account_id=account_id,
                        region=region,
                        availability_zone=_string_or_none(volume.get("AvailabilityZone")),
                        volume_id=volume_id,
                        state=state,
                        size_gib=_float_or_none(volume.get("Size")),
                        tags=tags,
                        owner=owner,
                        owner_source=owner_source,
                        first_seen_available=snapshot_date,
                        source_created_at=_parse_datetime_or_none(volume.get("CreateTime")),
                        observed_at=observed_at,
                    )
                )
    return snapshots


def _scan_unattached_gcp_disks(
    *,
    projects: tuple[str, ...],
    snapshot_date: date,
    owner_label_keys: tuple[str, ...],
    disk_page_fetcher: GcpDiskPageFetcher | None,
    google_access_token: str | None,
) -> list[UnattachedBlockVolumeSnapshot]:
    observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    access_token = (
        None
        if disk_page_fetcher is not None
        else (google_access_token or _get_google_access_token())
    )
    snapshots: list[UnattachedBlockVolumeSnapshot] = []

    for project_id in projects:
        page_token: str | None = None
        while True:
            if disk_page_fetcher is None:
                assert access_token is not None
                page = _fetch_gcp_aggregated_disks_page(
                    project_id,
                    page_token,
                    access_token=access_token,
                )
            else:
                page = disk_page_fetcher(project_id, page_token)

            for scope, disk in _iter_gcp_disks(page):
                snapshot = _gcp_disk_snapshot(
                    project_id=project_id,
                    scope=scope,
                    disk=disk,
                    snapshot_date=snapshot_date,
                    owner_label_keys=owner_label_keys,
                    observed_at=observed_at,
                )
                if snapshot is not None:
                    snapshots.append(snapshot)

            next_page_token = str(page.get("nextPageToken") or "").strip()
            if not next_page_token:
                break
            page_token = next_page_token

    return snapshots


def _gcp_disk_snapshot(
    *,
    project_id: str,
    scope: str,
    disk: Mapping[str, Any],
    snapshot_date: date,
    owner_label_keys: tuple[str, ...],
    observed_at: datetime,
) -> UnattachedBlockVolumeSnapshot | None:
    if _has_attached_users(disk.get("users")):
        return None

    disk_name = str(disk.get("name") or "").strip()
    if not disk_name:
        return None

    availability_zone = _last_path_segment(disk.get("zone"))
    region = _last_path_segment(disk.get("region"))
    if not availability_zone and scope.startswith("zones/"):
        availability_zone = scope.split("/", 1)[1]
    if not region and scope.startswith("regions/"):
        region = scope.split("/", 1)[1]
    if not region and availability_zone:
        region = _region_from_zone(availability_zone)
    if not region:
        region = "global"

    labels = _string_mapping(disk.get("labels"))
    disk_type = _last_path_segment(disk.get("type"))
    if disk_type:
        labels.setdefault("disk_type", disk_type)
    owner, owner_source = _owner_from_tags(labels, owner_label_keys, prefix="label")

    return UnattachedBlockVolumeSnapshot(
        snapshot_date=snapshot_date,
        vendor="gcp",
        account_id=project_id,
        region=region,
        availability_zone=availability_zone,
        volume_id=disk_name,
        state=_string_or_none(disk.get("status")) or "unattached",
        size_gib=_float_or_none(disk.get("sizeGb")),
        tags=labels,
        owner=owner,
        owner_source=owner_source,
        first_seen_available=snapshot_date,
        source_created_at=_parse_datetime_or_none(disk.get("creationTimestamp")),
        observed_at=observed_at,
    )


def _iter_gcp_disks(page: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    items = page.get("items")
    if not isinstance(items, Mapping):
        return
    for scope, scoped_list in items.items():
        if not isinstance(scoped_list, Mapping):
            continue
        disks = scoped_list.get("disks")
        if not isinstance(disks, list):
            continue
        for disk in disks:
            if isinstance(disk, Mapping):
                yield str(scope), disk


def _fetch_gcp_aggregated_disks_page(
    project_id: str,
    page_token: str | None,
    *,
    access_token: str,
) -> dict[str, Any]:
    params = {"maxResults": "500"}
    if page_token:
        params["pageToken"] = page_token
    project = urllib_parse.quote(project_id, safe="")
    url = f"{COMPUTE_DISKS_API_TEMPLATE.format(project=project)}?{urllib_parse.urlencode(params)}"
    request = urllib_request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "ci-dashboard-sync-unattached-block-volumes",
        },
    )
    return _request_json(request, timeout=30, error_context="Compute Engine disks API")


def _load_existing_volume_dates(
    connection: Connection,
    *,
    snapshot_date: date,
) -> dict[tuple[str, str, str, str], ExistingBlockVolumeDates]:
    cutoff_date = snapshot_date - timedelta(days=UNATTACHED_BLOCK_VOLUME_RETENTION_DAYS)
    # Daily snapshots carry the earliest observed dates forward. If the job is
    # paused longer than retention, history can only be recovered from retained rows.
    rows = connection.execute(
        text(
            """
            SELECT
              vendor,
              account_id,
              region,
              volume_id,
              MIN(first_seen_available) AS first_seen_available,
              MIN(source_created_at) AS source_created_at
            FROM cost_unattached_block_volume_daily
            WHERE snapshot_date >= :cutoff_date
            GROUP BY vendor, account_id, region, volume_id
            """
        ),
        {"cutoff_date": cutoff_date},
    ).mappings()
    result: dict[tuple[str, str, str, str], ExistingBlockVolumeDates] = {}
    for row in rows:
        first_seen = _parse_date(row["first_seen_available"])
        if first_seen:
            key = (
                str(row["vendor"]),
                str(row["account_id"]),
                str(row["region"]),
                str(row["volume_id"]),
            )
            result[key] = ExistingBlockVolumeDates(
                first_seen_available=first_seen,
                source_created_at=_parse_datetime_or_none(row["source_created_at"]),
            )
    return result


def _delete_expired_snapshots(engine: Engine, *, snapshot_date: date) -> None:
    cutoff_date = snapshot_date - timedelta(days=UNATTACHED_BLOCK_VOLUME_RETENTION_DAYS)
    while True:
        with engine.begin() as connection:
            deleted_rows = _delete_expired_snapshot_batch(
                connection,
                cutoff_date=cutoff_date,
            )
        if deleted_rows < UNATTACHED_BLOCK_VOLUME_DELETE_BATCH_SIZE:
            break


def _delete_expired_snapshot_batch(connection: Connection, *, cutoff_date: date) -> int:
    if connection.dialect.name == "sqlite":
        statement = text(
            """
            DELETE FROM cost_unattached_block_volume_daily
            WHERE id IN (
              SELECT id
              FROM cost_unattached_block_volume_daily
              WHERE snapshot_date < :cutoff_date
              LIMIT :limit
            )
            """
        )
    else:
        statement = text(
            """
            DELETE FROM cost_unattached_block_volume_daily
            WHERE snapshot_date < :cutoff_date
            LIMIT :limit
            """
        )
    result = connection.execute(
        statement,
        {
            "cutoff_date": cutoff_date,
            "limit": UNATTACHED_BLOCK_VOLUME_DELETE_BATCH_SIZE,
        },
    )
    return int(result.rowcount or 0)


def _with_existing_volume_dates(
    snapshot: UnattachedBlockVolumeSnapshot,
    existing_dates: dict[tuple[str, str, str, str], ExistingBlockVolumeDates],
) -> UnattachedBlockVolumeSnapshot:
    existing = existing_dates.get(
        (snapshot.vendor, snapshot.account_id, snapshot.region, snapshot.volume_id)
    )
    if existing is None:
        return snapshot

    first_seen_available = min(
        existing.first_seen_available,
        snapshot.first_seen_available,
    )
    source_created_at = _min_datetime(
        existing.source_created_at,
        snapshot.source_created_at,
    )
    if (
        first_seen_available == snapshot.first_seen_available
        and source_created_at == snapshot.source_created_at
    ):
        return snapshot
    return replace(
        snapshot,
        first_seen_available=first_seen_available,
        source_created_at=source_created_at,
    )


def _upsert_snapshots(
    connection: Connection,
    snapshots: Iterable[UnattachedBlockVolumeSnapshot],
) -> None:
    snapshots = list(snapshots)
    if not snapshots:
        return
    statement = _upsert_statement(connection)
    connection.execute(
        statement,
        [_snapshot_params(snapshot) for snapshot in snapshots],
    )


def _upsert_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            """
            INSERT INTO cost_unattached_block_volume_daily (
              snapshot_date, vendor, account_id, region, availability_zone,
              volume_id, state, size_gib, tags_json, owner, owner_source,
              first_seen_available, source_created_at, observed_at, updated_at
            ) VALUES (
              :snapshot_date, :vendor, :account_id, :region, :availability_zone,
              :volume_id, :state, :size_gib, :tags_json, :owner, :owner_source,
              :first_seen_available, :source_created_at, :observed_at, CURRENT_TIMESTAMP
            )
            ON CONFLICT(snapshot_date, vendor, account_id, region, volume_id)
            DO UPDATE SET
              availability_zone = excluded.availability_zone,
              state = excluded.state,
              size_gib = excluded.size_gib,
              tags_json = excluded.tags_json,
              owner = excluded.owner,
              owner_source = excluded.owner_source,
              first_seen_available = excluded.first_seen_available,
              source_created_at = excluded.source_created_at,
              observed_at = excluded.observed_at,
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        """
        INSERT INTO cost_unattached_block_volume_daily (
          snapshot_date, vendor, account_id, region, availability_zone,
          volume_id, state, size_gib, tags_json, owner, owner_source,
          first_seen_available, source_created_at, observed_at
        ) VALUES (
          :snapshot_date, :vendor, :account_id, :region, :availability_zone,
          :volume_id, :state, :size_gib, CAST(:tags_json AS JSON), :owner, :owner_source,
          :first_seen_available, :source_created_at, :observed_at
        )
        ON DUPLICATE KEY UPDATE
          availability_zone = VALUES(availability_zone),
          state = VALUES(state),
          size_gib = VALUES(size_gib),
          tags_json = VALUES(tags_json),
          owner = VALUES(owner),
          owner_source = VALUES(owner_source),
          first_seen_available = VALUES(first_seen_available),
          source_created_at = VALUES(source_created_at),
          observed_at = VALUES(observed_at),
          updated_at = CURRENT_TIMESTAMP
        """
    )


def _snapshot_params(
    snapshot: UnattachedBlockVolumeSnapshot,
) -> dict[str, Any]:
    tags_json = json.dumps(snapshot.tags, ensure_ascii=False, sort_keys=True)
    return {
        "snapshot_date": snapshot.snapshot_date,
        "vendor": snapshot.vendor,
        "account_id": snapshot.account_id,
        "region": snapshot.region,
        "availability_zone": snapshot.availability_zone,
        "volume_id": snapshot.volume_id,
        "state": snapshot.state,
        "size_gib": snapshot.size_gib,
        "tags_json": tags_json,
        "owner": snapshot.owner,
        "owner_source": snapshot.owner_source,
        "first_seen_available": snapshot.first_seen_available,
        "source_created_at": snapshot.source_created_at,
        "observed_at": snapshot.observed_at,
    }


def _resolve_aws_account_id() -> str:
    client = _build_sts_client()
    identity = client.get_caller_identity()
    account_id = str(identity.get("Account") or "").strip()
    if not account_id:
        raise ValueError("AWS STS get_caller_identity did not return an account id")
    return account_id


def _build_ec2_client(region: str):
    boto3 = _import_boto3()
    return boto3.client("ec2", region_name=region)


def _build_sts_client():
    boto3 = _import_boto3()
    return boto3.client("sts")


def _import_boto3():
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("boto3 is required for sync-unattached-block-volumes") from exc
    return boto3


def _get_google_access_token() -> str:
    from_env = (os.environ.get("CI_DASHBOARD_GCP_ACCESS_TOKEN") or "").strip()
    if from_env:
        return from_env

    request = urllib_request.Request(
        METADATA_TOKEN_URL,
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            token = str(payload.get("access_token") or "").strip()
            if token:
                return token
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("Unable to fetch GCP access token from metadata server") from exc
    raise RuntimeError("Metadata server token response missing access_token")


def _request_json(
    request: urllib_request.Request,
    *,
    timeout: int,
    error_context: str,
) -> dict[str, Any]:
    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        try:
            with urllib_request.urlopen(request, timeout=timeout) as response:
                return _decode_json_object(response.read(), error_context=error_context)
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if attempt < API_RETRY_ATTEMPTS and exc.code in RETRYABLE_HTTP_STATUS_CODES:
                _sleep_before_retry(attempt)
                continue
            raise RuntimeError(f"{error_context} request failed: HTTP {exc.code}: {body}") from exc
        except urllib_error.URLError as exc:
            if attempt < API_RETRY_ATTEMPTS:
                _sleep_before_retry(attempt)
                continue
            raise RuntimeError(f"{error_context} request failed: {exc.reason}") from exc
    raise AssertionError("unreachable")


def _decode_json_object(body: bytes, *, error_context: str) -> dict[str, Any]:
    decoded = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as exc:
        snippet = " ".join(decoded.split()) or "<empty body>"
        raise RuntimeError(f"{error_context} returned invalid JSON: {snippet[:200]}") from exc
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"{error_context} response is not an object")


def _sleep_before_retry(attempt: int) -> None:
    time.sleep(API_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))


def _aws_tags_to_dict(raw_tags: Any) -> dict[str, str]:
    tags: dict[str, str] = {}
    if not isinstance(raw_tags, list):
        return tags
    for item in raw_tags:
        if not isinstance(item, dict):
            continue
        key = str(item.get("Key") or "").strip()
        value = str(item.get("Value") or "").strip()
        if key and value:
            tags[key] = value
    return tags


def _string_mapping(raw_value: Any) -> dict[str, str]:
    if not isinstance(raw_value, Mapping):
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_item in raw_value.items():
        key = str(raw_key or "").strip()
        value = str(raw_item or "").strip()
        if key and value:
            result[key] = value
    return result


def _owner_from_tags(
    tags: dict[str, str],
    owner_tag_keys: tuple[str, ...],
    *,
    prefix: str,
) -> tuple[str | None, str | None]:
    lower_tags = {key.lower(): (key, value) for key, value in tags.items()}
    for configured_key in owner_tag_keys:
        match = lower_tags.get(configured_key.lower())
        if match is None:
            continue
        raw_key, value = match
        value = value.strip()
        if value:
            return value, f"{prefix}:{raw_key}"
    return None, None


def _has_attached_users(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(str(item or "").strip() for item in value)
    return bool(str(value).strip())


def _last_path_segment(value: Any) -> str | None:
    text_value = str(value or "").strip().rstrip("/")
    if not text_value:
        return None
    return text_value.rsplit("/", 1)[-1] or None


def _region_from_zone(zone: str) -> str:
    parts = zone.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else zone


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text_value = str(value).strip()
    if not text_value:
        return None
    normalized = text_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed_date = _parse_date(text_value)
        return datetime.combine(parsed_date, datetime.min.time()) if parsed_date else None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _min_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _string_or_none(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value or None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
