"""Opt-in, whitelist-only Kubernetes evidence. No usage collection or raw objects.

The small SQL sink intentionally duplicates the fixed Cost Insight guard protocol:
these are independently installed modules, not an importable shared application.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, DecimalException
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ci_dashboard.jobs.build_url_matcher import normalize_build_url, normalized_job_path_from_key
from ci_dashboard.jobs.sync_pods import (
    PodMetadataSnapshot,
    _coerce_str_mapping,
    _extract_normalized_build_url_from_metadata,
)

SOURCE = "tencent:ap-beijing:cls-9zpon9y1"
CLUSTER = "cls-9zpon9y1"
ACCOUNT = "100050658403"
GUARD_KEY = f"utilization:tencent:{ACCOUNT}"
BEIJING = ZoneInfo("Asia/Shanghai")
ATTRIBUTES = frozenset({
    "repo", "job_name", "node_name", "cpu_request", "memory_request", "cpu_limit",
    "memory_limit", "cpu_capacity", "memory_capacity", "allocatable_cpu",
    "allocatable_memory", "instance_type", "resource_class", "created_at", "ended_at",
    "metadata_confidence",
})


def enabled(source: str) -> bool:
    return source == SOURCE and os.getenv("CI_DASHBOARD_UTILIZATION_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def week_bounds(instant: datetime) -> tuple[date, datetime, datetime]:
    local = instant.replace(tzinfo=UTC).astimezone(BEIJING)
    week = local.date() - timedelta(days=local.weekday())
    start = datetime.combine(week, datetime.min.time(), BEIJING).astimezone(UTC).replace(tzinfo=None)
    return week, start + timedelta(days=7), start + timedelta(days=14)


def quantity(value: Any) -> float:
    """Kubernetes Quantity decimal/binary SI, normalized to cores or bytes."""
    match = re.fullmatch(r"([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([numkKMGTEP]|[KMGTPE]i)?", str(value))
    if not match:
        raise ValueError("invalid Kubernetes resource quantity")
    suffix = match[2] or ""
    scales = {"n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3")}
    if suffix.endswith("i"):
        scale = Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
    else:
        scale = scales.get(suffix, Decimal(1000) ** ("kMGTPE".find(suffix.replace("K", "k")) + 1) if suffix else Decimal(1))
    try:
        result = float(Decimal(match[1]) * scale)
    except (DecimalException, OverflowError) as exc:
        raise ValueError("invalid Kubernetes resource quantity") from exc
    if not 0 <= result < float("inf"):
        raise ValueError("non-finite resource quantity")
    return result


def effective_resources(spec: dict[str, Any]) -> dict[str, Any]:
    """Scheduler max(init peaks, app + restartable init) plus Pod overhead.

    Pod-level resources and in-place resize status require version-aware semantics;
    reject those denominators explicitly rather than guessing. Limits absent on any
    concurrently active container remain unbounded, not zero.
    """
    if spec.get("resources"):
        raise ValueError("unsupported_pod_level_resources")
    containers = spec.get("containers")
    if not isinstance(containers, list) or not containers:
        raise ValueError("missing_containers")
    init = spec.get("initContainers") or []
    result: dict[str, Any] = {}
    for resource, prefix in (("cpu", "cpu"), ("memory", "memory")):
        for kind, output in (("requests", "request"), ("limits", "limit")):
            def amount(container):
                resources = container.get("resources") or {}
                raw = (resources.get(kind) or {}).get(resource)
                # API defaulting normally fills request=limit. Also honor it for offline objects.
                if raw is None and kind == "requests":
                    raw = (resources.get("limits") or {}).get(resource)
                value = (0.0 if kind == "requests" else float("inf")) if raw is None else quantity(raw)
                return float("inf") if kind == "limits" and value == 0 else value

            app = sum(amount(container) for container in containers)
            sidecars = 0.0
            peak = 0.0
            for container in init:
                current = amount(container)
                if container.get("restartPolicy") == "Always":
                    sidecars += current
                    peak = max(peak, sidecars)
                else:
                    peak = max(peak, sidecars + current)
            effective = max(app + sidecars, peak)
            overhead = quantity((spec.get("overhead") or {}).get(resource, 0))
            effective += overhead
            scale = 1000 if resource == "cpu" else 1
            result[f"{prefix}_{output}"] = None if effective == float("inf") else math.ceil(effective * scale) / scale
    return result


def stable_job(build_url: str | None) -> str:
    normalized = normalize_build_url(build_url, default_host="https://do.pingcap.net")
    if not normalized:
        return "unknown_job"
    path = urlparse(normalized).path
    # Only a verified Jenkins job path ending in a numeric run is stable evidence.
    if not re.fullmatch(r"/jenkins(?:-staging)?/(?:job/[^/]+/)+\d+/", path):
        return "unknown_job"
    job_segments = path.strip("/").split("/")[2:-1:2]
    if any(re.fullmatch(r"(?i:(?:pr|pull|pull-request)[-_]?)?\d+", unquote(segment)) for segment in job_segments):
        return "unknown_job"
    return urlparse(normalized_job_path_from_key(normalized)).path


def normalize_pod(pod: dict[str, Any], *, linkage: dict[str, Any] | None = None) -> dict[str, Any] | None:
    meta = pod.get("metadata") or {}
    if not all(isinstance(meta.get(key), str) and meta[key] for key in ("uid", "namespace", "name")):
        return None
    linkage = linkage or {}
    labels = _coerce_str_mapping(meta.get("labels"))
    annotations = _coerce_str_mapping(meta.get("annotations"))
    org = labels.get("org") or labels.get("prow.k8s.io/refs.org")
    repo = labels.get("repo") or labels.get("prow.k8s.io/refs.repo")
    repo = linkage.get("repo_full_name") or (f"{org}/{repo}" if org and repo else "unknown_repo")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        repo = "unknown_repo"
    build_url = linkage.get("normalized_build_url") or _extract_normalized_build_url_from_metadata(
        PodMetadataSnapshot(pod_uid=meta["uid"], labels=labels, annotations=annotations, observed_at=None),
        cloud_phase="TENCENT",
    )
    spec = pod.get("spec") or {}
    attrs = {"repo": repo, "job_name": stable_job(build_url), "node_name": spec.get("nodeName") or "",
             "resource_class": "tke_pod", "metadata_confidence": "observed"}
    try:
        status = pod.get("status") or {}
        if status.get("resize") or any(
            item.get("allocatedResources") or item.get("resources")
            for item in status.get("containerStatuses", []) + status.get("initContainerStatuses", [])
        ):
            raise ValueError("unsupported_in_place_resize")
        attrs.update(effective_resources(spec))
    except (ValueError, TypeError, AttributeError) as exc:
        attrs.update(cpu_request=None, memory_request=None, cpu_limit=None, memory_limit=None,
                     metadata_confidence=f"requests_unknown:{exc}"[:128])
    if meta.get("creationTimestamp"):
        attrs["created_at"] = meta["creationTimestamp"]
    return {"resource_type": "pod", "resource_id": meta["uid"], "cluster": CLUSTER,
            "namespace": meta["namespace"], "name": meta["name"], "provider_id": "", "attributes_json": attrs}


def normalize_node(node: dict[str, Any]) -> dict[str, Any] | None:
    meta = node.get("metadata") or {}
    provider = (node.get("spec") or {}).get("providerID") or ""
    if not meta.get("uid") or not meta.get("name"):
        return None
    status = node.get("status") or {}
    labels = meta.get("labels") or {}
    attrs = {"resource_class": "tke_node", "metadata_confidence": "sampled_node_identity_unverified",
             "instance_type": labels.get("node.kubernetes.io/instance-type") or ""}
    # Keep raw provider identity; only a validated Tencent provider may identify a physical machine.
    if re.fullmatch(r"qcloud:///ap-beijing/(?:ins|np)-[a-zA-Z0-9-]+", provider):
        attrs["metadata_confidence"] = "sampled"
    try:
        for resource, prefix in (("cpu", "cpu"), ("memory", "memory")):
            attrs[f"{prefix}_capacity"] = quantity(status["capacity"][resource])
            attrs[f"allocatable_{prefix}"] = quantity(status["allocatable"][resource])
    except (KeyError, ValueError, TypeError):
        attrs["metadata_confidence"] = "capacity_unknown"
        for key in ("cpu_capacity", "memory_capacity", "allocatable_cpu", "allocatable_memory"):
            attrs.pop(key, None)
    return {"resource_type": "node", "resource_id": meta["uid"], "cluster": CLUSTER, "namespace": "",
            "name": meta["name"], "provider_id": provider, "attributes_json": attrs}


class MetadataExporter:
    """Only in-memory live identities; finite intervals end at last confirmed evidence.

    reset() on a Pod WATCH gap/relist discards continuity, never backdates a new
    snapshot. Clean EOF resumes the last WATCH cursor. Only healthy WATCH
    heartbeats or unchanged, fresh Node LIST samples carry confirmed-live evidence
    across a week boundary.
    """
    def __init__(self, engine: Engine):
        self.engine = engine
        self.live: dict[str, dict[str, Any]] = {}
        self.last_heartbeat: datetime | None = None

    def reset(self) -> None:
        self.live.clear()
        self.last_heartbeat = None

    def observe(self, records: list[dict[str, Any]], *, at: datetime | None = None, deleted: bool = False,
                heartbeat: bool = False, now: datetime | None = None) -> None:
        try:
            # Read the database BEFORE making identities: observed_from is an upsert
            # key, so clamping/rekeying it later in the sink would break renewal.
            clock = database_now(self.engine, now=now)
            skew = abs((_datetime(at) if at is not None else utc_now()) - clock)
            if skew > timedelta(seconds=60):
                raise ValueError(f"utilization observation clock skew exceeds 60s: {skew.total_seconds():.3f}s")
            self._observe(records, at=clock, deleted=deleted, heartbeat=heartbeat, now=now)
        except Exception:
            # A dropped batch is a continuity gap, including a failed clock read.
            self.reset()
            raise

    def _observe(self, records: list[dict[str, Any]], *, at: datetime, deleted: bool,
                 heartbeat: bool, now: datetime | None) -> None:
        week, week_end, expiry = week_bounds(at)
        week_start = week_end - timedelta(days=7)
        # Expired identities must never be renewed, nor poison a fresh baseline.
        self.live = {key: row for key, row in self.live.items() if at < row["expires_at"]}
        updates: list[dict[str, Any]] = []
        # A complete, uninterrupted Pod WATCH confirms all known live UIDs. Renew
        # at most once a minute, except the first heartbeat after a week boundary.
        if heartbeat and (self.last_heartbeat is None or (at - self.last_heartbeat).total_seconds() >= 60
                          or week_bounds(self.last_heartbeat)[0] != week):
            self.last_heartbeat = at
            for key, current in self.live.items():
                if current["resource_type"] != "pod":
                    continue  # Sampled Nodes need their own current LIST confirmation.
                current["observed_until"] = min(at, week_bounds(current["observed_from"])[1])
                current["last_seen"] = current["observed_until"]
                updates.append(dict(current))
                if current["week_start"] != week:
                    current = {**current, "week_start": week, "expires_at": expiry,
                               "observed_from": week_start, "observed_until": at, "last_seen": at}
                    self.live[key] = current
                    updates.append(dict(current))
        for record in records:
            key = record["resource_id"]
            old = self.live.get(key)
            if deleted and old is None:
                # Postmortem snapshots prove no live interval. Closed UIDs leave
                # live immediately, so repeats need neither rows nor an ended cache.
                continue
            attrs = record["attributes_json"]
            fingerprint = hashlib.sha256(json.dumps(attrs, sort_keys=True).encode()).hexdigest()
            same_identity = old and all(old[field] == record[field] for field in (
                "resource_type", "cluster", "namespace", "name", "provider_id",
            ))
            continuous = same_identity and old["fingerprint"] == fingerprint and (
                record["resource_type"] != "node" or timedelta(0) <= at - old["last_seen"] <= timedelta(minutes=35)
            )
            if continuous and old["week_start"] == week:
                old.update(observed_until=at, last_seen=at)
                row = old
            else:
                if old:
                    # A sampled change cannot locate the transition inside the poll
                    # gap; only identical, fresh samples may bridge it. Pod changes
                    # retain the ordered WATCH stream's transition-at-event policy.
                    until = (old["last_seen"] if old["resource_type"] == "node" and not continuous
                             else min(at, week_bounds(old["observed_from"])[1]))
                    old.update(observed_until=until, last_seen=until)
                    updates.append(dict(old))
                start = week_start if continuous and record["resource_type"] == "node" else at
                row = {**record, "vendor": "tencent", "account_id": ACCOUNT, "week_start": week,
                       "expires_at": expiry, "observed_from": start, "observed_until": at,
                       "last_seen": at, "fingerprint": fingerprint}
                self.live[key] = row
            if deleted:
                row["attributes_json"] = {**attrs, "ended_at": at.isoformat()}
                row["fingerprint"] = hashlib.sha256(json.dumps(row["attributes_json"], sort_keys=True).encode()).hexdigest()
                self.live.pop(key, None)
            updates.append(dict(row))
        # Match the sink's final-value rule before batching too: a rollover at
        # exactly week start may share its key with a transition/deletion here.
        updates = list({(row["resource_type"], row["resource_id"], row["observed_from"]): row for row in updates}.values())
        for offset in range(0, len(updates), 1000):
            save_observations(self.engine, updates[offset:offset + 1000],
                              **({"now": now} if now is not None else {}))


def database_now(engine: Engine, *, now: datetime | None = None) -> datetime:
    """Observation baseline only; the sink still locks/rechecks guard and DB time."""
    with engine.connect() as connection:
        if now is not None and connection.dialect.name != "sqlite":
            raise ValueError("clock override is restricted to offline SQLite tests")
        return _datetime(now if now is not None else connection.execute(text(
            "SELECT CURRENT_TIMESTAMP" if connection.dialect.name == "sqlite" else "SELECT UTC_TIMESTAMP(6)"
        )).scalar_one())


def _datetime(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _admit(state: dict, clock: datetime, weeks: set[date], projected: int) -> None:
    """Guard v1; keep synchronized with cost_insight.utilization.policy.health."""
    def fresh(field, max_age=14400):
        value = state.get(field)
        return value is not None and -60 <= (clock - _datetime(value)).total_seconds() <= max_age

    size = state.get("observed_bytes")
    if (state.get("version") != 1 or not state.get("enabled") or state.get("paused")
            or not state.get("counts_reliable") or not fresh("reconciled_at", 26 * 3600)
            or not fresh("maintenance_at")
            or not fresh("bytes_observed_at") or not isinstance(size, (int, float))
            or not 0 <= size < 8 * 1024**3
            or not isinstance(state.get("retained_rows"), int)
            or state["retained_rows"] < 0 or not 0 <= projected < 2_000_000):
        raise ValueError("utilization guard disabled, stale, unreliable or over budget")
    if state.get("oldest_expired_at") and (clock - _datetime(state["oldest_expired_at"])).total_seconds() > 14400:
        raise ValueError("utilization expiry backlog")
    current, _, _ = week_bounds(clock)
    for week in weeks:
        if (week not in {current, current - timedelta(days=7)}
                or week < date.fromisoformat(state["collection_start_week"])
                or (state.get("collection_stop_week") and week > date.fromisoformat(state["collection_stop_week"]))
                or (state.get("finalized_through_week") and week <= date.fromisoformat(state["finalized_through_week"]))
                or clock >= datetime.combine(week + timedelta(days=14), datetime.min.time(), BEIJING).astimezone(UTC).replace(tzinfo=None)):
            raise ValueError("utilization week expired, closed or outside collection calendar")


def _prepare(row: dict, clock: datetime) -> dict:
    if row.get("vendor", "tencent") != "tencent" or row.get("account_id", ACCOUNT) != ACCOUNT:
        raise ValueError("wrong utilization account")
    if row.get("resource_type") not in {"pod", "node"} or row.get("cluster") != CLUSTER:
        raise ValueError("unsupported utilization resource")
    start = _datetime(row["observed_from"])
    until = _datetime(row["observed_until"])
    seen = _datetime(row["last_seen"])
    week, end, expiry = week_bounds(start)
    if not start <= until <= seen <= clock or seen > end:
        raise ValueError("invalid or unbounded observation interval")
    if row.get("expires_at", expiry) != expiry or row.get("week_start", week) != week:
        raise ValueError("observation expiry is immutable")
    attrs = row["attributes_json"]
    if attrs.keys() - ATTRIBUTES or len(json.dumps(attrs, allow_nan=False).encode()) > 8192:
        raise ValueError("non-whitelisted or oversized evidence")
    for key, value in attrs.items():
        if key.startswith(("cpu_", "memory_", "allocatable_")):
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                raise ValueError("invalid evidence resource quantity")
        elif not isinstance(value, str):
            raise ValueError("invalid evidence attribute")
    if attrs.get("ended_at") and not start <= _datetime(attrs["ended_at"]) <= until:
        raise ValueError("invalid observation end evidence")
    if len(attrs.get("repo", "")) > 255 or len(attrs.get("job_name", "")) > 512:
        raise ValueError("oversized repo/job identity; never truncate")
    item = {**row, "vendor": "tencent", "account_id": ACCOUNT, "week_start": week,
            "expires_at": expiry, "observed_from": start, "observed_until": until, "last_seen": seen}
    for key, limit in (("resource_id", 255), ("cluster", 128), ("namespace", 128), ("name", 255), ("provider_id", 255)):
        value = item.get(key, "")
        if not isinstance(value, str) or len(value) > limit or (key == "resource_id" and not value):
            raise ValueError("invalid resource identity; never truncate")
        item[key] = value
    fingerprint = item.get("fingerprint")
    if fingerprint != hashlib.sha256(json.dumps(attrs, sort_keys=True).encode()).hexdigest():
        raise ValueError("invalid observation fingerprint")
    return item


def save_observations(engine: Engine, observations: list[dict], *, now=None, dry_run=False) -> dict:
    """Finite interval upserts + shared retained-row admission in one transaction.

    No schema creation, implicit configuration, retries/backlog or cloud calls.
    dry-run issues only SELECT, including when the guard has never been configured.
    """
    if len(observations) > 1000:
        raise ValueError("observation batch exceeds 1000")
    with engine.connect() as connection:
        if now is not None and connection.dialect.name != "sqlite":
            raise ValueError("clock override is restricted to offline SQLite tests")
        if not dry_run:
            if connection.dialect.name != "sqlite":
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            connection.exec_driver_sql("BEGIN IMMEDIATE" if connection.dialect.name == "sqlite" else "BEGIN PESSIMISTIC")
        try:
            suffix = " FOR UPDATE" if not dry_run and connection.dialect.name != "sqlite" else ""
            value = connection.execute(text(
                "SELECT watermark_json FROM cost_job_state WHERE job_name = :key" + suffix
            ), {"key": GUARD_KEY}).scalar()
            state = value if isinstance(value, dict) else json.loads(value) if value else {}
            if dry_run:
                return {"dry_run": True, "rows": len(observations), "configured": bool(state)}
            clock = _datetime(now if now is not None else connection.execute(text(
                "SELECT CURRENT_TIMESTAMP" if connection.dialect.name == "sqlite" else "SELECT UTC_TIMESTAMP(6)"
            )).scalar_one())
            prepared = [_prepare(row, clock) for row in observations]
            # One final value per logical interval (heartbeats and changes may share it).
            prepared = list({(row["resource_type"], row["resource_id"], row["observed_from"]): row for row in prepared}.values())
            _admit(state, clock, {row["week_start"] for row in prepared}, state.get("retained_rows", 0))
            pending = []
            inserted = 0
            predicate = ("vendor = :vendor AND account_id = :account_id AND week_start = :week_start "
                         "AND resource_type = :resource_type AND resource_id = :resource_id AND observed_from = :observed_from")
            for row in prepared:
                previous = connection.execute(text(
                    "SELECT id, expires_at, fingerprint, attributes_json, observed_until FROM cost_utilization_resource_observation WHERE " + predicate
                ), row).mappings().first()
                if previous:
                    before = previous["attributes_json"]
                    before = before if isinstance(before, dict) else json.loads(before)
                    after = row["attributes_json"]
                    if (_datetime(previous["expires_at"]) != row["expires_at"]
                            or (before.get("ended_at") and before.get("ended_at") != after.get("ended_at"))
                            or {k: v for k, v in before.items() if k != "ended_at"} != {k: v for k, v in after.items() if k != "ended_at"}
                            or (previous["observed_until"] and _datetime(previous["observed_until"]) > row["observed_until"])):
                        raise ValueError("conflicting or out-of-order evidence")
                else:
                    inserted += 1
                overlap = connection.execute(text(
                    "SELECT id FROM cost_utilization_resource_observation "
                    "WHERE vendor=:vendor AND account_id=:account_id AND week_start=:week_start AND resource_type=:resource_type "
                    "AND resource_id=:resource_id AND observed_from != :observed_from "
                    "AND observed_from < :observed_until AND observed_until > :observed_from LIMIT 1"
                ), row).first()
                if overlap:
                    raise ValueError("overlapping evidence from an out-of-order writer")
                pending.append((row, previous))
            _admit(state, clock, {row["week_start"] for row in prepared}, state["retained_rows"] + inserted)
            for row, previous in pending:
                params = {**row, "attributes_json": json.dumps(row["attributes_json"], allow_nan=False)}
                json_value = ":attributes_json" if connection.dialect.name == "sqlite" else "CAST(:attributes_json AS JSON)"
                if previous:
                    connection.execute(text(
                        "UPDATE cost_utilization_resource_observation SET observed_until=:observed_until, "
                        f"last_seen=:last_seen, fingerprint=:fingerprint, attributes_json={json_value} WHERE id=:id"
                    ), {**params, "id": previous["id"]})
                else:
                    columns = ("vendor", "account_id", "week_start", "expires_at", "resource_type", "resource_id",
                               "cluster", "namespace", "name", "provider_id", "observed_from", "observed_until",
                               "last_seen", "fingerprint", "attributes_json")
                    values = [json_value if col == "attributes_json" else f":{col}" for col in columns]
                    connection.execute(text(
                        f"INSERT INTO cost_utilization_resource_observation ({','.join(columns)}) VALUES ({','.join(values)})"
                    ), params)
            state["retained_rows"] += inserted
            json_state = ":state" if connection.dialect.name == "sqlite" else "CAST(:state AS JSON)"
            connection.execute(text(f"UPDATE cost_job_state SET watermark_json={json_state} WHERE job_name=:key"),
                               {"state": json.dumps(state), "key": GUARD_KEY})
            # Re-read DB time at commit so a slow writer cannot cross the hard expiry fence.
            if now is None:
                commit_clock = _datetime(connection.execute(text(
                    "SELECT CURRENT_TIMESTAMP" if connection.dialect.name == "sqlite" else "SELECT UTC_TIMESTAMP(6)"
                )).scalar_one())
                _admit(state, commit_clock, {row["week_start"] for row in prepared}, state["retained_rows"])
            connection.commit()
            return {"rows": inserted, "updated": len(prepared) - inserted}
        except BaseException:
            connection.rollback()
            raise
