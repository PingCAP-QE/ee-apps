"""Opt-in Pod collection context; no cloud calls, usage samples or raw objects.

Independent packages deliberately duplicate this small fixed SQL guard protocol.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ci_dashboard.jobs.build_url_matcher import normalize_build_url, normalized_job_path_from_key
from ci_dashboard.jobs.sync_pods import (
    PodMetadataSnapshot,
    _coerce_str_mapping,
    _extract_normalized_build_url_from_metadata,
    _parse_datetime,
)

SOURCE = "tencent:ap-beijing:cls-9zpon9y1"
CLUSTER = "cls-9zpon9y1"
ACCOUNT = "100050658403"
GUARD_KEY = f"utilization:tencent:{ACCOUNT}"
LABELS = frozenset({"repo", "job", "test_project", "test_suite", "environment", "project", "service"})
PERSONAL_KEYS = frozenset({"author", "owner", "email", "user", "username", "employee", "employee_id"})


def label_keys(keys):
    if (not isinstance(keys, list) or not 1 <= len(keys) <= 32
            or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,62}", key)
                   or key.lower() in PERSONAL_KEYS for key in keys)
            or len(set(keys)) != len(keys)):
        raise ValueError("bounded non-personal configured label keys required")
    return set(keys)


CONTEXT_TTL = timedelta(hours=24)
STALE_AFTER = timedelta(minutes=15)
NODE_SELECTOR_MAX_NODES = 10000
NODE_SELECTOR_REFRESH_SECONDS = 60
POD_FIELDS = (
    "account_id", "cluster_id", "namespace", "pod_uid", "pod_name", "node_query_kind",
    "node_query_value", "cpu_request_cores", "memory_request_bytes", "labels_json",
)


def enabled(source: str) -> bool:
    return source == SOURCE and os.getenv("CI_DASHBOARD_UTILIZATION_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def quantity(value: Any) -> Decimal:
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
        result = Decimal(match[1]) * scale
    except (DecimalException, OverflowError) as exc:
        raise ValueError("invalid Kubernetes resource quantity") from exc
    if not math.isfinite(result):
        raise ValueError("non-finite resource quantity")
    return result


def effective_resources(spec: dict[str, Any]) -> dict[str, float]:
    """Scheduler max(init peaks, app + restartable init) plus Pod overhead.

    Read limits only for Kubernetes request defaulting; never persist them.
    Unsupported Pod-level/in-place resize semantics remain unknown.
    """
    if spec.get("resources"):
        raise ValueError("unsupported_pod_level_resources")
    containers = spec.get("containers")
    if not isinstance(containers, list) or not containers:
        raise ValueError("missing_containers")
    result = {}
    for resource, field in (("cpu", "cpu_request_cores"), ("memory", "memory_request_bytes")):
        def amount(container):
            resources = container.get("resources") or {}
            raw = (resources.get("requests") or {}).get(resource)
            if raw is None:
                raw = (resources.get("limits") or {}).get(resource, 0)
            return quantity(raw)

        app = sum(amount(container) for container in containers)
        sidecars = peak = Decimal(0)
        for container in spec.get("initContainers") or []:
            current = amount(container)
            if container.get("restartPolicy") == "Always":
                sidecars += current
                peak = max(peak, sidecars)
            else:
                peak = max(peak, sidecars + current)
        effective = max(app + sidecars, peak) + quantity((spec.get("overhead") or {}).get(resource, 0))
        scale = 1000 if resource == "cpu" else 1
        result[field] = math.ceil(effective * scale) / scale
    return result


def stable_job(build_url: str | None) -> str:
    normalized = normalize_build_url(build_url, default_host="https://do.pingcap.net")
    if not normalized:
        return ""
    path = urlparse(normalized).path
    if not re.fullmatch(r"/jenkins(?:-staging)?/(?:job/[^/]+/)+\d+/", path):
        return ""
    job_segments = path.strip("/").split("/")[2:-1:2]
    if any(re.fullmatch(r"(?i:(?:pr|pull|pull-request)[-_]?)?\d+", unquote(segment)) for segment in job_segments):
        return ""
    return urlparse(normalized_job_path_from_key(normalized)).path


def _ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


@dataclass(frozen=True)
class _NodeIdentity:
    uid: str
    created: datetime
    instance_id: str


def normalize_node(node: dict[str, Any]) -> tuple[str, _NodeIdentity] | None:
    """A reusable nodeName or IP alone cannot identify a historical instance."""
    meta = node.get("metadata") or {}
    name, uid = meta.get("name"), meta.get("uid")
    if (not isinstance(name, str) or not 0 < len(name) <= 255 or not isinstance(uid, str)
            or not uid or meta.get("deletionTimestamp")):
        return None
    created = _parse_datetime(meta.get("creationTimestamp"))
    provider = (node.get("spec") or {}).get("providerID")
    if created is None or not isinstance(provider, str):
        return None
    match = re.fullmatch(r"(?:qcloud:///ap-beijing/)?((?:ins|np)-[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*)", provider)
    return (name, _NodeIdentity(uid, created, match[1])) if match else None


class NodeSelectorCache:
    """One bounded, thread-safe LIST snapshot shared by namespace WATCH workers."""
    def __init__(self, *, max_nodes=NODE_SELECTOR_MAX_NODES, ttl_seconds=2 * NODE_SELECTOR_REFRESH_SECONDS):
        self.max_nodes = max_nodes
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._selectors: dict[str, _NodeIdentity] = {}
        self._listed_at = float("-inf")

    def refresh(self, nodes: list[dict], *, listed_at: float) -> None:
        if len(nodes) > self.max_nodes:
            self.clear()
            raise ValueError("Node selector cache exceeds bound")
        selectors = dict(record for node in nodes if (record := normalize_node(node)) is not None)
        with self._lock:
            if time.monotonic() - self._listed_at <= self.ttl_seconds:
                for name, node in selectors.items():
                    previous = self._selectors.get(name)
                    if previous == node:
                        # Object identity is a continuity token. A missing row,
                        # clear, expiry or changed mapping ends that chain.
                        selectors[name] = previous
            self._selectors = selectors
            self._listed_at = listed_at

    def clear(self) -> None:
        with self._lock:
            self._selectors = {}
            self._listed_at = float("-inf")

    def snapshot(self) -> tuple[float, dict[str, _NodeIdentity]]:
        # The published dict is only replaced, never mutated.
        with self._lock:
            if time.monotonic() - self._listed_at <= self.ttl_seconds:
                return self._listed_at, self._selectors
        return float("-inf"), {}


@dataclass(frozen=True)
class PodNodeRef:
    name: str
    assigned_at: datetime
    seen_at: float


def pod_node_ref(pod: dict[str, Any], seen_at: float) -> PodNodeRef | None:
    name = (pod.get("spec") or {}).get("nodeName")
    created = _parse_datetime((pod.get("metadata") or {}).get("creationTimestamp"))
    if not isinstance(name, str) or not name or created is None:
        return None
    for condition in (pod.get("status") or {}).get("conditions") or ():
        if (isinstance(condition, dict) and condition.get("type") == "PodScheduled"
                and condition.get("status") == "True"):
            assigned = _parse_datetime(condition.get("lastTransitionTime"))
            if assigned is not None and assigned >= created:
                return PodNodeRef(name, assigned, seen_at)
    # Without scheduling evidence, require the Node to predate Pod creation.
    return PodNodeRef(name, created, seen_at)


def normalize_pod(pod: dict[str, Any], *, linkage: dict[str, Any] | None = None,
                  selected_labels: set[str] | frozenset[str] = LABELS) -> dict[str, Any] | None:
    meta = pod.get("metadata") or {}
    if not all(isinstance(meta.get(key), str) and meta[key] for key in ("uid", "namespace", "name")):
        return None
    linkage = linkage or {}
    raw_labels = _coerce_str_mapping(meta.get("labels"))
    annotations = _coerce_str_mapping(meta.get("annotations"))
    selected = selected_labels
    labels = {key: value for key, value in raw_labels.items() if key in selected and value}
    org = raw_labels.get("org") or raw_labels.get("prow.k8s.io/refs.org")
    repo = raw_labels.get("repo") or raw_labels.get("prow.k8s.io/refs.repo")
    repo = linkage.get("repo_full_name") or (f"{org}/{repo}" if org and repo else repo)
    if repo and "repo" in selected:
        labels["repo"] = repo
    build_url = linkage.get("normalized_build_url") or _extract_normalized_build_url_from_metadata(
        PodMetadataSnapshot(pod_uid=meta["uid"], labels=raw_labels, annotations=annotations, observed_at=None),
        cloud_phase="TENCENT",
    )
    job = stable_job(build_url)
    # Raw job labels may be run identifiers. Do not guess stable job names.
    labels.pop("job", None)
    if job and "job" in selected:
        labels["job"] = job
    spec = pod.get("spec") or {}
    requests = {"cpu_request_cores": None, "memory_request_bytes": None}
    try:
        status = pod.get("status") or {}
        if status.get("resize") or any(
            item.get("allocatedResources") or item.get("resources")
            for item in status.get("containerStatuses", []) + status.get("initContainerStatuses", [])
        ):
            raise ValueError("unsupported_in_place_resize")
        requests.update(effective_resources(spec))
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    return {"account_id": ACCOUNT, "cluster_id": CLUSTER, "namespace": meta["namespace"],
            "pod_uid": meta["uid"], "pod_name": meta["name"], "node_query_kind": "",
            "node_query_value": "", **requests, "labels_json": labels}


def context_key(record: dict, start: datetime) -> str:
    identity = [record["account_id"], record["cluster_id"], record["pod_uid"], start.isoformat(timespec="microseconds")]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


class MetadataExporter:
    """Finite confirmed intervals; reconnects never bridge missing evidence."""
    def __init__(self, engine: Engine, *, node_cache: NodeSelectorCache | None = None):
        configured = os.getenv("CI_DASHBOARD_UTILIZATION_LABEL_KEYS")
        self.selected_labels = label_keys(configured.split(",") if configured is not None else sorted(LABELS))
        self.engine = engine
        self.node_cache = node_cache
        self.live: dict[str, tuple[dict[str, Any], PodNodeRef | None, _NodeIdentity | None]] = {}
        self.last_heartbeat: datetime | None = None

    def reset(self) -> None:
        previous = [row for row, _, _ in self.live.values()]
        self.live.clear()
        self.last_heartbeat = None
        # Best effort only: failed exports leave finite evidence for maintenance's
        # 15-minute orphan fence. Never queue/replay failed observations.
        try:
            for offset in range(0, len(previous), 1000):
                save_contexts(self.engine, [self._closed(row) for row in previous[offset:offset + 1000]])
        except Exception:
            pass

    @staticmethod
    def _closed(row: dict) -> dict:
        return {**row, "closed_at": row["observed_until"], "expires_at": row["observed_until"] + CONTEXT_TTL}

    @staticmethod
    def _new_context(record: dict, start: datetime, until: datetime) -> dict:
        return {**record, "context_key": context_key(record, start), "observed_from": start,
                "observed_until": until, "closed_at": None, "planned_until": start,
                "expires_at": until + CONTEXT_TTL}

    def observe(self, records: list[dict[str, Any]], *, at: datetime | None = None, deleted: bool = False,
                heartbeat: bool = False, now: datetime | None = None) -> None:
        try:
            clock = database_now(self.engine, now=now)  # BEFORE constructing any identities.
            if abs((_datetime(at) if at is not None else utc_now()) - clock) > timedelta(seconds=60):
                raise ValueError("utilization observation clock skew exceeds 60s")
            current = dict(self.live)
            updates = {}
            for uid, (row, _, _) in self.live.items():
                if clock < row["observed_until"]:
                    raise ValueError("utilization database clock moved backwards")
                if clock - row["observed_until"] >= STALE_AFTER:
                    updates[row["context_key"]] = self._closed(row)
                    del current[uid]
            incoming = {row["pod_uid"]: row for row in records}
            confirm = heartbeat and (self.last_heartbeat is None or clock - self.last_heartbeat >= timedelta(seconds=60))
            listed_at, nodes = self.node_cache.snapshot() if self.node_cache is not None else (float("-inf"), {})
            for uid in (dict.fromkeys([*incoming, *current]) if confirm else incoming):
                old, ref, pinned = current.get(uid, (None, None, None))
                if deleted and uid in incoming:
                    if old is not None:
                        updates[old["context_key"]] = self._closed(old)
                        del current[uid]
                    continue  # Never-observed-live terminal snapshots prove no interval.
                if uid in incoming:
                    observed_ref = incoming[uid].get("_node_ref")
                    if (ref is None or observed_ref is None
                            or (ref.name, ref.assigned_at) != (observed_ref.name, observed_ref.assigned_at)):
                        ref = observed_ref
                source = incoming.get(uid, old)
                record = {field: source[field] for field in POD_FIELDS}
                candidate = nodes.get(ref.name) if ref is not None else None
                bound = old is not None and bool(old["node_query_value"])
                # A Pod object names a Node, not a Node UID. Use only a most-recent
                # LIST begun after that Pod evidence, and reject Nodes created at
                # or after scheduling: those could be replacements at the same name.
                if (candidate is not None and candidate.created < ref.assigned_at
                        and (pinned is candidate if bound else
                             listed_at > ref.seen_at and (pinned is None or pinned == candidate))):
                    pinned = candidate
                    record.update(node_query_kind="un_instance_id", node_query_value=pinned.instance_id)
                else:
                    record.update(node_query_kind="", node_query_value="")
                if old and all(old[field] == record[field] for field in POD_FIELDS):
                    row = {**old, "observed_until": clock, "expires_at": clock + CONTEXT_TTL}
                else:
                    start = clock
                    if old:
                        if clock == old["observed_from"]:
                            continue  # No room for a distinct interval at the same DB timestamp.
                        if bound and not record["node_query_value"]:
                            # Pod liveness still covers the gap, but neither the old
                            # selector nor newly observed metadata applies to it.
                            # Even a zero-length old row owns its start timestamp.
                            gap_start = max(old["observed_until"], old["observed_from"] + timedelta(microseconds=1))
                            updates[old["context_key"]] = self._closed(old)
                            unbound = {field: old[field] for field in POD_FIELDS}
                            unbound.update(node_query_kind="", node_query_value="")
                            if unbound == record:
                                start = gap_start
                            elif gap_start < clock:
                                gap = self._closed(self._new_context(unbound, gap_start, clock))
                                updates[gap["context_key"]] = gap
                        else:
                            updates[old["context_key"]] = self._closed({**old, "observed_until": clock})
                    row = self._new_context(record, start, clock)
                current[uid] = (row, ref, pinned)
                updates[row["context_key"]] = row
            pending = list(updates.values())
            for offset in range(0, len(pending), 1000):
                save_contexts(self.engine, pending[offset:offset + 1000], **({"now": now} if now is not None else {}))
            self.live = current
            if confirm:
                self.last_heartbeat = clock
        except Exception:
            self.live.clear()
            self.last_heartbeat = None
            raise


def _datetime(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _clock(connection, now=None) -> datetime:
    if now is not None and connection.dialect.name != "sqlite":
        raise ValueError("clock override is restricted to offline SQLite tests")
    return _datetime(now if now is not None else connection.execute(text(
        "SELECT CURRENT_TIMESTAMP" if connection.dialect.name == "sqlite" else "SELECT UTC_TIMESTAMP(6)"
    )).scalar_one())


def database_now(engine: Engine, *, now: datetime | None = None) -> datetime:
    with engine.connect() as connection:
        return _clock(connection, now)


def _admit(state: dict, clock: datetime, projected: int, rows: list[dict]) -> None:
    """V2 admission, synchronized with Cost Insight's shared guard."""
    def fresh(field):
        value = state.get(field)
        return value is not None and -60 <= (clock - _datetime(value)).total_seconds() <= 14400

    size = state.get("observed_bytes")
    count = state.get("retained_rows")
    if (state.get("version") != 2 or state.get("enabled") is not True or state.get("paused") is not False
            or state.get("counts_reliable") is not True or not fresh("maintenance_at")
            or not fresh("bytes_observed_at") or type(size) not in (int, float)
            or not 0 <= size < 8 * 1024**3 or type(count) is not int or count < 0
            or not 0 <= projected < 2_000_000):
        raise ValueError("utilization guard disabled, stale, unreliable or over budget")
    scope = state.get("scope") or {}
    labels = scope.get("labels")
    namespaces = scope.get("namespaces")
    if (scope.get("account_id") != ACCOUNT or scope.get("cluster_id") != CLUSTER
            or scope.get("cluster_region") != "ap-beijing" or "ap-beijing" not in scope.get("regions", [])
            or not isinstance(labels, list)
            or not isinstance(namespaces, list) or not namespaces):
        raise ValueError("utilization guard scope mismatch")
    approved = label_keys(labels)
    for row in rows:
        if row["namespace"] not in namespaces or not set(row["labels_json"]) <= approved:
            raise ValueError("Pod context outside approved guard scope")


def _prepare(row: dict, clock: datetime) -> dict:
    if row.get("account_id") != ACCOUNT or row.get("cluster_id") != CLUSTER:
        raise ValueError("wrong utilization account or cluster")
    allowed = set(POD_FIELDS) | {"context_key", "observed_from", "observed_until", "closed_at", "planned_until", "expires_at"}
    if set(row) != allowed:
        raise ValueError("non-whitelisted Pod context fields")
    item = dict(row)
    for field in ("observed_from", "observed_until", "planned_until", "expires_at", "closed_at"):
        item[field] = _datetime(row[field]) if row[field] is not None else None
    start, until, closed = item["observed_from"], item["observed_until"], item["closed_at"]
    if (not start <= item["planned_until"] <= until <= clock or (closed is not None and closed != until)
            or item["expires_at"] != until + CONTEXT_TTL or clock >= item["expires_at"]):
        raise ValueError("invalid or unbounded Pod context interval")
    if item["context_key"] != context_key(item, start):
        raise ValueError("invalid context identity")
    for field, limit in (("namespace", 128), ("pod_uid", 255), ("pod_name", 255), ("node_query_value", 255)):
        value = item[field]
        if not isinstance(value, str) or len(value) > limit or (field != "node_query_value" and not value):
            raise ValueError("invalid context identity; never truncate")
    kind, value = item["node_query_kind"], item["node_query_value"]
    if not ((kind == "" and value == "") or (kind == "node" and _ip(value) == value)
            or (kind == "un_instance_id" and re.fullmatch(r"(?:ins|np)-[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*", value))):
        raise ValueError("unverified Node monitor selector")
    for field in ("cpu_request_cores", "memory_request_bytes"):
        value = item[field]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("invalid request quantity")
    labels = item["labels_json"]
    if (not isinstance(labels, dict)
            or any(not isinstance(value, str) or not value or len(value) > 512 for value in labels.values())
            or len(json.dumps(labels, allow_nan=False).encode()) > 8192):
        raise ValueError("non-whitelisted or oversized labels")
    if labels:
        label_keys(list(labels))
    return item


def save_contexts(engine: Engine, contexts: list[dict], *, now=None) -> dict:
    """Locked immutable-context upserts + atomic shared counts, no network I/O."""
    if len(contexts) > 1000:
        raise ValueError("context batch exceeds 1000")
    with engine.connect() as connection:
        if now is not None and connection.dialect.name != "sqlite":
            raise ValueError("clock override is restricted to offline SQLite tests")
        if connection.dialect.name != "sqlite":
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        connection.exec_driver_sql("BEGIN IMMEDIATE" if connection.dialect.name == "sqlite" else "BEGIN PESSIMISTIC")
        try:
            suffix = " FOR UPDATE" if connection.dialect.name != "sqlite" else ""
            value = connection.execute(text(
                "SELECT watermark_json FROM cost_job_state WHERE job_name=:key" + suffix
            ), {"key": GUARD_KEY}).scalar()
            state = value if isinstance(value, dict) else json.loads(value) if value else {}
            clock = _clock(connection, now)
            if now is None and abs(utc_now() - clock) > timedelta(seconds=60):
                raise ValueError("utilization database clock skew exceeds 60s")
            prepared = [_prepare(row, clock) for row in contexts]
            prepared = list({row["context_key"]: row for row in prepared}.values())
            _admit(state, clock, state.get("retained_rows", 0), prepared)
            inserted = 0
            for row in prepared:
                previous = connection.execute(text(
                    "SELECT * FROM pod_utilization_context WHERE context_key=:context_key"
                ), row).mappings().first()
                if previous:
                    before = dict(previous)
                    before["labels_json"] = before["labels_json"] if isinstance(before["labels_json"], dict) else json.loads(before["labels_json"])
                    if (any(before[field] != row[field] for field in POD_FIELDS)
                            or _datetime(before["observed_from"]) != row["observed_from"]
                            or _datetime(before["observed_until"]) > row["observed_until"]):
                        raise ValueError("conflicting or out-of-order Pod context")
                    if before["closed_at"] is not None:
                        if (_datetime(before["closed_at"]) != row["closed_at"]
                                or _datetime(before["observed_until"]) != row["observed_until"]
                                or _datetime(before["expires_at"]) != row["expires_at"]):
                            raise ValueError("closed Pod context cannot renew")
                        continue
                overlap = connection.execute(text(
                    "SELECT id FROM pod_utilization_context WHERE account_id=:account_id AND cluster_id=:cluster_id "
                    "AND pod_uid=:pod_uid AND context_key!=:context_key "
                    "AND ((observed_from < :observed_until AND observed_until > :observed_from) "
                    "OR (:inserting=1 AND observed_from >= :observed_from)) LIMIT 1"
                ), {**row, "inserting": int(previous is None)}).first()
                if overlap:
                    raise ValueError("overlapping or out-of-order Pod context")
                if previous is None:
                    # A prior exporter may have lost DB access before closing its
                    # segment. Close only its already-confirmed evidence; never
                    # backfill the disconnected interval or renew its expiry.
                    connection.execute(text(
                        "UPDATE pod_utilization_context SET closed_at=observed_until "
                        "WHERE account_id=:account_id AND cluster_id=:cluster_id AND pod_uid=:pod_uid "
                        "AND closed_at IS NULL AND observed_from < :observed_from AND observed_until <= :observed_from"
                    ), row)
                params = {**row, "labels_json": json.dumps(row["labels_json"], sort_keys=True, allow_nan=False)}
                if previous:
                    # planned_until belongs to the dispatcher: never overwrite its progress.
                    connection.execute(text(
                        "UPDATE pod_utilization_context SET observed_until=:observed_until, closed_at=:closed_at, "
                        "expires_at=:expires_at WHERE context_key=:context_key"
                    ), params)
                else:
                    columns = tuple(row)
                    values = ["CAST(:labels_json AS JSON)" if field == "labels_json" and connection.dialect.name != "sqlite"
                              else f":{field}" for field in columns]
                    connection.execute(text(
                        f"INSERT INTO pod_utilization_context ({','.join(columns)}) VALUES ({','.join(values)})"
                    ), params)
                    inserted += 1
            state["retained_rows"] += inserted
            commit_clock = _clock(connection, now)
            if commit_clock < clock:
                raise ValueError("utilization database clock moved backwards")
            if now is None and abs(utc_now() - commit_clock) > timedelta(seconds=60):
                raise ValueError("utilization database clock skew exceeds 60s")
            _admit(state, commit_clock, state["retained_rows"], prepared)
            if any(commit_clock >= row["expires_at"] for row in prepared):
                raise ValueError("Pod context expired before commit")
            json_state = ":state" if connection.dialect.name == "sqlite" else "CAST(:state AS JSON)"
            connection.execute(text(f"UPDATE cost_job_state SET watermark_json={json_state} WHERE job_name=:key"),
                               {"state": json.dumps(state), "key": GUARD_KEY})
            connection.commit()
            return {"rows": inserted, "updated": len(prepared) - inserted}
        except BaseException:
            connection.rollback()
            raise
