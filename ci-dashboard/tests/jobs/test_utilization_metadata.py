from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text

from ci_dashboard.jobs import pod_watcher as watcher
from ci_dashboard.jobs import utilization_metadata as u

NOW = datetime(2026, 11, 24, 4)


def pod():
    return {"metadata": {"uid": "pod-uid", "namespace": "jenkins-tidb", "name": "random-pod-3290",
                         "creationTimestamp": "2026-11-01T00:00:00Z",
                         "labels": {"org": "pingcap", "repo": "tidb", "author": "private-person", "owner": "private"},
                         "annotations": {"buildUrl": "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_mysql_test/3290/", "email": "private@example.com"}},
            "spec": {"nodeName": "node-a", "containers": [{"resources": {"requests": {"cpu": "2100m", "memory": "2Gi"}, "limits": {"cpu": "4", "memory": "4Gi"}}}]}}


def node():
    return {"metadata": {"uid": "node-uid", "name": "node"},
            "spec": {"providerID": "qcloud:///ap-beijing/np-123"},
            "status": {"capacity": {"cpu": "8", "memory": "16Gi"},
                       "allocatable": {"cpu": "7800m", "memory": "15Gi"}}}


def row(at=NOW):
    value = u.normalize_pod(pod())
    week, _, expiry = u.week_bounds(at)
    return {**value, "vendor": "tencent", "account_id": u.ACCOUNT, "week_start": week,
            "expires_at": expiry, "observed_from": at, "observed_until": at,
            "last_seen": at, "fingerprint": hashlib.sha256(json.dumps(value["attributes_json"], sort_keys=True).encode()).hexdigest()}


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE cost_job_state (job_name TEXT PRIMARY KEY, watermark_json TEXT)")
        c.exec_driver_sql("""CREATE TABLE cost_utilization_resource_observation (
            id INTEGER PRIMARY KEY, vendor TEXT, account_id TEXT, week_start DATE, expires_at DATETIME,
            resource_type TEXT, resource_id TEXT, cluster TEXT, namespace TEXT, name TEXT, provider_id TEXT,
            observed_from DATETIME, observed_until DATETIME, last_seen DATETIME, fingerprint TEXT, attributes_json TEXT,
            UNIQUE(vendor,account_id,week_start,resource_type,resource_id,observed_from))""")
        c.execute(text("INSERT INTO cost_job_state VALUES (:key,:state)"), {"key": u.GUARD_KEY, "state": json.dumps(state())})
    yield engine
    engine.dispose()


def state(**changes):
    return {"version": 1, "enabled": True, "paused": False, "counts_reliable": True,
            "retained_rows": 0, "observed_bytes": 0, "maintenance_at": NOW.isoformat(),
            "bytes_observed_at": NOW.isoformat(), "oldest_expired_at": None, "reconciled_at": NOW.isoformat(),
            "collection_start_week": "2026-11-02", "collection_stop_week": None,
            "finalized_through_week": None, **changes}


def replace_state(engine, **changes):
    with engine.begin() as c:
        c.execute(text("UPDATE cost_job_state SET watermark_json=:state"), {"state": json.dumps(state(**changes))})


def rows(engine):
    with engine.connect() as c:
        return list(c.execute(text("SELECT * FROM cost_utilization_resource_observation ORDER BY observed_from")).mappings())


def retained(engine):
    with engine.connect() as c:
        return json.loads(c.execute(text("SELECT watermark_json FROM cost_job_state")).scalar_one())["retained_rows"]


def test_opt_in_only_exact_source(monkeypatch):
    monkeypatch.delenv("CI_DASHBOARD_UTILIZATION_ENABLED", raising=False)
    assert not u.enabled(u.SOURCE)
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")
    assert u.enabled(u.SOURCE)
    assert not u.enabled("gcp:other")


@pytest.mark.parametrize(("raw", "expected"), [("100m", .1), ("1Gi", 1024**3), ("1e3", 1000), ("2M", 2_000_000), ("1u", 1e-6), ("1n", 1e-9), ("2Ki", 2048)])
def test_quantity(raw, expected):
    assert u.quantity(raw) == expected


@pytest.mark.parametrize("raw", ["NaN", "-1", "Infinity", "garbage", "1GiB", "1e9999999"])
def test_invalid_quantity(raw):
    with pytest.raises(ValueError):
        u.quantity(raw)


def test_scheduler_init_sidecar_overhead_and_unbounded_limits():
    spec = {"containers": [{"resources": {"requests": {"cpu": "2", "memory": "1Gi"}}}],
            "initContainers": [
                {"restartPolicy": "Always", "resources": {"requests": {"cpu": "500m", "memory": "128Mi"}}},
                {"resources": {"requests": {"cpu": "4", "memory": "2Gi"}}}],
            "overhead": {"cpu": "100m", "memory": "64Mi"}}
    effective = u.effective_resources(spec)
    assert effective == {"cpu_request": 4.6, "memory_request": 2240 * 1024**2, "cpu_limit": None, "memory_limit": None}
    # The later sidecar must not be counted during an earlier ordinary init.
    spec["initContainers"].reverse()
    assert u.effective_resources(spec)["cpu_request"] == 4.1


def test_scheduler_defaulting_rounding_and_zero_limits():
    spec = {"containers": [{"resources": {"limits": {"cpu": "0.0001", "memory": "0.1"}}}]}
    assert u.effective_resources(spec) == {"cpu_request": .001, "cpu_limit": .001, "memory_request": 1, "memory_limit": 1}
    assert u.effective_resources({"containers": [{}]})["cpu_request"] == 0
    assert u.effective_resources({"containers": [{"resources": {"limits": {"cpu": "0"}}}]})["cpu_limit"] is None


@pytest.mark.parametrize("kind", ["pod_level", "resize", "bad_quantity"])
def test_explicit_quality_rejection(kind):
    value = pod()
    if kind == "pod_level":
        value["spec"]["resources"] = {"requests": {"cpu": "8"}}
    elif kind == "resize":
        value["status"] = {"containerStatuses": [{"allocatedResources": {"cpu": "1"}}]}
    else:
        value["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "bad"
    attrs = u.normalize_pod(value)["attributes_json"]
    assert attrs["cpu_request"] is None
    assert attrs["metadata_confidence"].startswith("requests_unknown:")
    assert attrs["repo"] == "pingcap/tidb"


def test_stable_job_and_whitelist_without_person():
    record = u.normalize_pod(pod(), linkage={"normalized_build_url": pod()["metadata"]["annotations"]["buildUrl"], "repo_full_name": "pingcap/tidb", "pod_author": "secret"})
    attrs = record["attributes_json"]
    assert attrs["job_name"] == "/jenkins/job/pingcap/job/tidb/job/ghpr_mysql_test/"
    assert set(attrs) <= u.ATTRIBUTES
    assert "private" not in json.dumps(record) and "secret" not in json.dumps(record)
    assert "3290" not in attrs["job_name"]
    assert u.stable_job("https://prow.tidb.net/view/gs/logs/random/123/") == "unknown_job"
    assert u.stable_job("random-pod-3290") == "unknown_job"
    assert u.stable_job("https://do.pingcap.net/job/repo/job/PR-123/3290/") == "unknown_job"
    value = pod()
    value["metadata"]["labels"] = {"prow.k8s.io/refs.org": "pingcap", "prow.k8s.io/refs.repo": "tidb"}
    assert u.normalize_pod(value)["attributes_json"]["repo"] == "pingcap/tidb"
    assert u.normalize_pod({}) is None


def test_node_normalization_whitelist_and_unknown_capacity():
    node = {"metadata": {"uid": "node-uid", "name": "node", "labels": {"owner": "person", "node.kubernetes.io/instance-type": "S5"}},
            "spec": {"providerID": "qcloud:///ap-beijing/np-123"},
            "status": {"capacity": {"cpu": "8", "memory": "16Gi"}, "allocatable": {"cpu": "7800m", "memory": "15Gi"}}}
    record = u.normalize_node(node)
    assert record["attributes_json"]["allocatable_cpu"] == 7.8
    assert "person" not in json.dumps(record)
    assert record["attributes_json"]["metadata_confidence"] == "sampled"
    node["spec"]["providerID"] = "unverified"
    assert "unverified" in u.normalize_node(node)["attributes_json"]["metadata_confidence"]
    node["status"] = {}
    assert u.normalize_node(node)["attributes_json"]["metadata_confidence"] == "capacity_unknown"
    assert u.normalize_node({}) is None


def test_intervals_identical_change_delete_gap_and_no_backdating(db, monkeypatch):
    export = u.MetadataExporter(db)
    export.observe([u.normalize_pod(pod())], at=NOW, now=NOW)
    export.observe([u.normalize_pod(pod())], at=NOW + timedelta(minutes=1), now=NOW + timedelta(minutes=1))
    assert len(rows(db)) == retained(db) == 1
    assert str(rows(db)[0]["observed_from"]).startswith("2026-11-24 04:00:00")
    changed = pod()
    changed["spec"]["nodeName"] = "node-b"
    changed["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "3"
    export.observe([u.normalize_pod(changed)], at=NOW + timedelta(minutes=2), now=NOW + timedelta(minutes=2), heartbeat=True)
    assert len(rows(db)) == retained(db) == 2
    assert json.loads(rows(db)[1]["attributes_json"])["cpu_request"] == 3
    export.observe([u.normalize_pod(changed)], at=NOW + timedelta(minutes=3), now=NOW + timedelta(minutes=3), deleted=True, heartbeat=True)
    assert not export.live
    assert json.loads(rows(db)[1]["attributes_json"])["ended_at"] == (NOW + timedelta(minutes=3)).isoformat()
    export.reset()
    export.observe([u.normalize_pod(changed)], at=NOW + timedelta(minutes=10), now=NOW + timedelta(minutes=10))
    assert len(rows(db)) == 3
    assert str(rows(db)[1]["observed_until"]).startswith("2026-11-24 04:03:00")
    assert str(rows(db)[2]["observed_from"]).startswith("2026-11-24 04:10:00")


def test_lost_delete_does_not_extend_old_evidence_on_relist(db, monkeypatch):
    first = NOW - timedelta(minutes=20)
    replace_state(db, maintenance_at=first.isoformat(), bytes_observed_at=first.isoformat(), reconciled_at=first.isoformat())
    export = u.MetadataExporter(db)
    export.observe([u.normalize_pod(pod())], at=first, now=first)
    export.reset()
    export.observe([], at=NOW, now=NOW)
    assert str(rows(db)[0]["observed_until"]).startswith("2026-11-24 03:40:00")


def test_export_failure_drops_live_state(db, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("offline")
    monkeypatch.setattr(u, "save_observations", fail)
    export = u.MetadataExporter(db)
    with pytest.raises(RuntimeError):
        export.observe([u.normalize_pod(pod())], at=NOW, now=NOW)
    assert not export.live


def test_beijing_week_and_confirmed_pod_cross_week_slice(db):
    at = datetime(2026, 11, 22, 15, 59)
    assert u.week_bounds(at)[0].isoformat() == "2026-11-16"
    assert u.week_bounds(at)[2] == datetime(2026, 11, 29, 16)
    assert u.week_bounds(at + timedelta(minutes=1))[0].isoformat() == "2026-11-23"
    replace_state(db, maintenance_at=at.isoformat(), bytes_observed_at=at.isoformat(), reconciled_at=at.isoformat())
    export = u.MetadataExporter(db)
    export.observe([u.normalize_pod(pod())], at=at, now=at)
    export.observe([u.normalize_pod(pod())], at=at + timedelta(minutes=2), now=at + timedelta(minutes=2), heartbeat=True)
    saved = rows(db)
    assert len(saved) == retained(db) == 2
    assert str(saved[0]["observed_until"]).startswith("2026-11-22 16:00:00")
    assert str(saved[1]["observed_from"]).startswith("2026-11-22 16:00:00")
    assert str(saved[0]["expires_at"]).startswith("2026-11-29 16:00:00")
    assert str(saved[1]["expires_at"]).startswith("2026-12-06 16:00:00")


@pytest.mark.parametrize("change", ["capacity", "provider_id", "name"])
@pytest.mark.parametrize("cross_week", [False, True])
def test_sampled_node_change_leaves_unknown_poll_gap(db, change, cross_week):
    first = datetime(2026, 11, 22, 15, 45) if cross_week else NOW
    replace_state(db, maintenance_at=first.isoformat(), bytes_observed_at=first.isoformat(), reconciled_at=first.isoformat())
    exporter = u.MetadataExporter(db)
    original = node()
    exporter.observe([u.normalize_node(original)], at=first, now=first)
    confirmed = first + timedelta(minutes=10)
    exporter.observe([u.normalize_node(original)], at=confirmed, now=confirmed)
    changed = copy.deepcopy(original)
    if change == "capacity":
        changed["status"]["capacity"]["cpu"] = "16"
    elif change == "provider_id":
        changed["spec"]["providerID"] = "qcloud:///ap-beijing/np-456"
    else:
        changed["metadata"]["name"] = "renamed-node"
    later = confirmed + timedelta(minutes=30)
    exporter.observe([u.normalize_node(changed)], at=later, now=later, heartbeat=True)
    saved = rows(db)
    assert len(saved) == retained(db) == 2
    assert u._datetime(saved[0]["observed_from"]) == first
    assert u._datetime(saved[0]["observed_until"]) == u._datetime(saved[0]["last_seen"]) == confirmed
    assert u._datetime(saved[1]["observed_from"]) == later
    assert u._datetime(saved[0]["expires_at"]) == u.week_bounds(first)[2]
    assert u._datetime(saved[1]["expires_at"]) == u.week_bounds(later)[2]


@pytest.mark.parametrize("minutes", [30, 35])
def test_unchanged_fresh_node_slices_continuously_across_week(db, minutes):
    boundary = datetime(2026, 11, 22, 16)
    first = boundary - timedelta(minutes=15)
    later = first + timedelta(minutes=minutes)
    replace_state(db, maintenance_at=first.isoformat(), bytes_observed_at=first.isoformat(), reconciled_at=first.isoformat())
    exporter = u.MetadataExporter(db)
    record = u.normalize_node(node())
    exporter.observe([record], at=first, now=first)
    exporter.observe([record], at=later, now=later)
    saved = rows(db)
    assert len(saved) == retained(db) == 2
    assert u._datetime(saved[0]["observed_from"]) == first
    assert u._datetime(saved[0]["observed_until"]) == u._datetime(saved[0]["last_seen"]) == boundary
    assert u._datetime(saved[1]["observed_from"]) == boundary
    assert u._datetime(saved[1]["observed_until"]) == u._datetime(saved[1]["last_seen"]) == later
    assert u._datetime(saved[0]["expires_at"]) == datetime(2026, 11, 29, 16)
    assert u._datetime(saved[1]["expires_at"]) == datetime(2026, 12, 6, 16)
    again = later + timedelta(minutes=30)
    exporter.observe([record], at=again, now=again)
    assert len(rows(db)) == retained(db) == 2
    assert u._datetime(rows(db)[1]["observed_until"]) == again
    assert u._datetime(rows(db)[0]["expires_at"]) == datetime(2026, 11, 29, 16)
    assert u._datetime(rows(db)[1]["expires_at"]) == datetime(2026, 12, 6, 16)


@pytest.mark.parametrize("cross_week", [False, True])
def test_stale_node_confirmation_starts_now_without_bridging(db, cross_week):
    first = datetime(2026, 11, 22, 15, 45) if cross_week else NOW
    replace_state(db, maintenance_at=first.isoformat(), bytes_observed_at=first.isoformat(), reconciled_at=first.isoformat())
    exporter = u.MetadataExporter(db)
    record = u.normalize_node(node())
    exporter.observe([record], at=first, now=first)
    later = first + timedelta(minutes=35, microseconds=1)
    exporter.observe([record], at=later, now=later)
    saved = rows(db)
    assert len(saved) == retained(db) == 2
    assert u._datetime(saved[0]["observed_until"]) == first
    assert u._datetime(saved[1]["observed_from"]) == later


def test_node_heartbeat_without_current_list_cannot_confirm_or_roll(db):
    first = datetime(2026, 11, 22, 15, 59)
    replace_state(db, maintenance_at=first.isoformat(), bytes_observed_at=first.isoformat(), reconciled_at=first.isoformat())
    exporter = u.MetadataExporter(db)
    exporter.observe([u.normalize_node(node())], at=first, now=first)
    later = first + timedelta(minutes=2)
    exporter.observe([], at=later, now=later, heartbeat=True)
    assert len(rows(db)) == retained(db) == 1
    assert u._datetime(rows(db)[0]["observed_until"]) == first


@pytest.mark.parametrize("resource", ["pod", "node"])
def test_reset_relist_cross_week_never_backdates_any_uid(db, resource):
    first = datetime(2026, 11, 22, 15, 59)
    replace_state(db, maintenance_at=first.isoformat(), bytes_observed_at=first.isoformat(), reconciled_at=first.isoformat())
    exporter = u.MetadataExporter(db)
    record = u.normalize_pod(pod()) if resource == "pod" else u.normalize_node(node())
    missing = {**record, "resource_id": "not-in-relist"}
    exporter.observe([record, missing], at=first, now=first, heartbeat=True)
    exporter.reset()  # The same boundary used by relist/reconnect/gap callers.
    later = first + timedelta(minutes=2)
    exporter.observe([record], at=later, now=later, heartbeat=True)
    exporter.observe([], at=later + timedelta(minutes=1), now=later + timedelta(minutes=1), heartbeat=True)
    saved = rows(db)
    assert len(saved) == retained(db) == 3
    assert all(u._datetime(value["observed_until"]) == first for value in saved[:2])
    assert saved[2]["resource_id"] == record["resource_id"]
    assert u._datetime(saved[2]["observed_from"]) == later
    assert set(exporter.live) == {record["resource_id"]}


@pytest.mark.parametrize("individual_event", [False, True])
def test_healthy_pod_heartbeat_rolls_all_live_uids_even_inside_throttle(db, individual_event):
    boundary = datetime(2026, 11, 22, 16)
    first = boundary - timedelta(seconds=10)
    replace_state(db, maintenance_at=first.isoformat(), bytes_observed_at=first.isoformat(), reconciled_at=first.isoformat())
    exporter = u.MetadataExporter(db)
    records = [{**u.normalize_pod(pod()), "resource_id": f"pod-{number}"} for number in range(3)]
    ended = {**records[0], "resource_id": "deleted-pod"}
    exporter.observe([*records, ended], at=first, now=first, heartbeat=True)
    exporter.observe([ended], at=first + timedelta(seconds=1), now=first + timedelta(seconds=1), deleted=True, heartbeat=True)
    later = boundary + timedelta(seconds=10)
    # A BOOKMARK or another Pod's event proves the complete live set, not just that UID.
    incoming = copy.deepcopy(records[:1]) if individual_event else []
    if incoming:
        incoming[0]["attributes_json"]["cpu_request"] = 3
    exporter.observe(incoming, at=later, now=later, heartbeat=True)
    saved = rows(db)
    previous = [value for value in saved if str(value["week_start"]) == "2026-11-16"]
    current = [value for value in saved if str(value["week_start"]) == "2026-11-23"]
    assert len(previous) == 4
    assert set(exporter.live) == {value["resource_id"] for value in records}
    assert {value["resource_id"] for value in current} == set(exporter.live)
    for value in previous:
        until = first + timedelta(seconds=1) if value["resource_id"] == "deleted-pod" else boundary
        assert u._datetime(value["observed_until"]) == until
        assert u._datetime(value["expires_at"]) == datetime(2026, 11, 29, 16)
    for value in current:
        assert u._datetime(value["observed_until"]) == u._datetime(value["last_seen"]) == later
        assert u._datetime(value["expires_at"]) == datetime(2026, 12, 6, 16)
        assert u._datetime(value["observed_from"]) in {boundary, later}
    rolled = [value for value in current if u._datetime(value["observed_from"]) == boundary]
    assert len(rolled) == 3
    assert all(json.loads(value["attributes_json"])["cpu_request"] == 2.1 for value in rolled)
    if individual_event:
        changed = [value for value in current if u._datetime(value["observed_from"]) == later]
        assert len(changed) == 1 and json.loads(changed[0]["attributes_json"])["cpu_request"] == 3


def test_boundary_rollover_with_attribute_transition_keeps_sink_batches_bounded(db, monkeypatch):
    boundary = datetime(2026, 11, 22, 16)
    first = boundary - timedelta(seconds=10)
    replace_state(db, maintenance_at=first.isoformat(), bytes_observed_at=first.isoformat(), reconciled_at=first.isoformat())
    exporter = u.MetadataExporter(db)
    records = [{**u.normalize_pod(pod()), "resource_id": f"pod-{number}"} for number in range(501)]
    exporter.observe(records, at=first, now=first, heartbeat=True)
    saved = u.save_observations
    batches = []
    def save(engine, observations, **kwargs):
        batches.append(len(observations))
        return saved(engine, observations, **kwargs)
    monkeypatch.setattr(u, "save_observations", save)
    changed = copy.deepcopy(records[0])
    changed["attributes_json"]["cpu_request"] = 3
    exporter.observe([changed], at=boundary, now=boundary, heartbeat=True)
    assert batches == [1000, 2]
    observed = rows(db)
    assert len(observed) == retained(db) == 1002
    current = [value for value in observed if str(value["week_start"]) == "2026-11-23"]
    assert len(current) == 501
    assert all(u._datetime(value["observed_from"]) == boundary for value in current)
    changed_row = next(value for value in current if value["resource_id"] == changed["resource_id"])
    assert json.loads(changed_row["attributes_json"])["cpu_request"] == 3


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"paused": True}, {"counts_reliable": False}, {"version": 2},
    {"maintenance_at": None}, {"maintenance_at": (NOW - timedelta(hours=5)).isoformat()},
    {"bytes_observed_at": (NOW - timedelta(hours=5)).isoformat()}, {"observed_bytes": None},
    {"reconciled_at": None}, {"reconciled_at": (NOW - timedelta(hours=27)).isoformat()},
    {"observed_bytes": 8 * 1024**3}, {"retained_rows": 1_999_999},
    {"oldest_expired_at": (NOW - timedelta(hours=5)).isoformat()},
    {"collection_stop_week": "2026-11-16"}, {"finalized_through_week": "2026-11-23"},
])
def test_guard_fail_closed(db, changes):
    replace_state(db, **changes)
    with pytest.raises(ValueError):
        u.save_observations(db, [row()], now=NOW)
    assert not rows(db)


def test_missing_guard_and_dry_run_issue_select_only(db):
    with db.begin() as c:
        c.exec_driver_sql("DELETE FROM cost_job_state")
    statements = []
    event.listen(db, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement))
    assert u.save_observations(db, [row()], now=NOW, dry_run=True)["configured"] is False
    assert all(sql.startswith("SELECT") for sql in statements)
    with pytest.raises(ValueError):
        u.save_observations(db, [row()], now=NOW)
    assert not rows(db)


def test_immutable_expiry_validation_and_transaction_rollback(db):
    u.save_observations(db, [row()], now=NOW)
    tampered = row()
    tampered["expires_at"] += timedelta(days=1)
    with pytest.raises(ValueError, match="immutable"):
        u.save_observations(db, [tampered], now=NOW)
    bad = copy.deepcopy(row())
    bad["attributes_json"]["author"] = "person"
    with pytest.raises(ValueError):
        u.save_observations(db, [bad], now=NOW)
    with db.begin() as c:
        c.exec_driver_sql("CREATE TRIGGER reject_guard_update BEFORE UPDATE ON cost_job_state BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
    other = row(NOW - timedelta(minutes=1))
    with pytest.raises(Exception, match="offline failure"):
        u.save_observations(db, [other], now=NOW)
    assert len(rows(db)) == retained(db) == 1


def test_stale_writer_cannot_overlap_newer_interval(db):
    original = row(NOW - timedelta(minutes=10))
    u.save_observations(db, [original], now=NOW)
    newer = row(NOW - timedelta(minutes=5))
    newer["observed_until"] = NOW
    newer["last_seen"] = NOW
    u.save_observations(db, [newer], now=NOW)
    original["observed_until"] = NOW
    original["last_seen"] = NOW
    with pytest.raises(ValueError, match="overlapping"):
        u.save_observations(db, [original], now=NOW)
    assert len(rows(db)) == retained(db) == 2


def test_expired_and_oversized_identity_are_rejected(db):
    with pytest.raises(ValueError):
        u.save_observations(db, [row(NOW - timedelta(days=15))], now=NOW)
    record = row()
    record["resource_id"] = "x" * 256
    with pytest.raises(ValueError, match="never truncate"):
        u.save_observations(db, [record], now=NOW)
    record = row()
    record["attributes_json"]["repo"] = "x" * 9000
    with pytest.raises(ValueError, match="oversized"):
        u.save_observations(db, [record], now=NOW)


def test_node_pagination_reuses_token_provider_and_ca(monkeypatch):
    tokens = iter(["first", "rotated"])
    calls = []
    def get(url, *, headers, ca_file):
        calls.append((url, headers, ca_file))
        return {"items": [{"metadata": {"name": str(len(calls))}}], "metadata": {"continue": "next" if len(calls) == 1 else "", "resourceVersion": "rv"}}
    monkeypatch.setattr(watcher, "_get_json", get)
    client = watcher._KubernetesClient(base_url="https://kube", token_provider=lambda: next(tokens), ca_file="existing-ca", watch_timeout_seconds=300)
    items, rv = client.list_nodes()
    assert len(items) == 2 and rv == "rv"
    assert calls[0][0] == "https://kube/api/v1/nodes?limit=500"
    assert "continue=next" in calls[1][0] and "/namespaces/" not in calls[1][0]
    assert calls[0][1] == {"Authorization": "Bearer first"}
    assert calls[1][1] == {"Authorization": "Bearer rotated"}
    assert calls[1][2] == "existing-ca"


def test_node_worker_lists_once_then_waits_thirty_minutes(monkeypatch):
    calls = []
    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            calls.append(seconds)
            self.stopped = True
    class Client:
        def list_nodes(self):
            calls.append("list")
            return [], "rv"
    watcher._run_utilization_node_worker(engine=None, client=Client(), stop_event=Stop())
    assert calls == ["list", 1800]


def test_enabled_pod_worker_preserves_deletion_and_resets_relist(monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")
    stop = threading.Event()
    saved = []
    resets = []
    ticks = iter(NOW + timedelta(seconds=i) for i in range(20))
    monkeypatch.setattr(u, "utc_now", lambda: NOW)
    monkeypatch.setattr(u, "database_now", lambda engine, **kwargs: next(ticks))
    monkeypatch.setattr(u, "save_observations", lambda engine, records: saved.extend(copy.deepcopy(records)))
    reset = u.MetadataExporter.reset
    def track_reset(self):
        resets.append(True)
        reset(self)
    monkeypatch.setattr(u.MetadataExporter, "reset", track_reset)
    class Client:
        lists = 0
        def list_collection(self, **kwargs):
            self.lists += 1
            if self.lists == 2:
                stop.set()
            return [pod()], "rv"
        def stream_collection(self, **kwargs):
            yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "next"}}}
            yield {"type": "DELETED", "object": pod()}
            yield {"type": "ERROR", "object": {"code": 410, "reason": "Expired"}}
    lifecycle_calls = []
    def persist(engine, settings, snapshots, **kwargs):
        lifecycle_calls.append(len(snapshots))
        kwargs["on_lifecycle_rows"]([{"pod_uid": item.pod_uid} for item in snapshots])
        return len(snapshots)
    monkeypatch.setattr(watcher, "_persist_pod_snapshots", persist)
    class Recorder:
        def add(self, **kwargs):
            pass
    watcher._run_pod_worker(
        engine=None, settings=None, client=Client(),
        context=watcher.WatchRuntimeContext(u.SOURCE, u.CLUSTER, "ap-beijing", "TENCENT"),
        namespace_name="jenkins-tidb", stream_key="pods", health_state=watcher.WatchHealthState(stale_after_seconds=60),
        recorder=Recorder(), jenkins_prefix_cache=None, stop_event=stop,
    )
    assert lifecycle_calls == [1, 1, 1]
    assert len(resets) == 4
    assert any("ended_at" in item["attributes_json"] for item in saved)
    assert saved[0]["observed_from"] == NOW
    assert saved[-1]["observed_from"] > NOW
    assert "private-person" not in json.dumps(saved, default=str)


def test_export_guard_failure_does_not_interrupt_existing_watcher(db, monkeypatch):
    replace_state(db, enabled=False)
    exporter = u.MetadataExporter(db)
    watcher._export_utilization(exporter, [u.normalize_pod(pod())])
    assert not exporter.live and not rows(db)


@pytest.mark.parametrize("skew_seconds", [.5, 30, 60, -60])
def test_observation_uses_database_clock_before_identity_and_never_rekeys(db, skew_seconds):
    exporter = u.MetadataExporter(db)
    record = u.normalize_pod(pod())
    exporter.observe([record], at=NOW + timedelta(seconds=skew_seconds), now=NOW)
    later = NOW + timedelta(minutes=1)
    exporter.observe([record], at=later + timedelta(seconds=skew_seconds), now=later, heartbeat=True)
    saved = rows(db)
    assert len(saved) == retained(db) == 1
    assert u._datetime(saved[0]["observed_from"]) == NOW
    assert u._datetime(saved[0]["observed_until"]) == u._datetime(saved[0]["last_seen"]) == later
    exporter.observe([record], at=later + timedelta(seconds=skew_seconds), now=later, deleted=True)
    assert not exporter.live
    assert json.loads(rows(db)[0]["attributes_json"])["ended_at"] == later.isoformat()


@pytest.mark.parametrize("skew_seconds", [60.000001, -60.000001, 3600])
def test_gross_clock_skew_is_explicit_and_drops_continuity(db, skew_seconds):
    exporter = u.MetadataExporter(db)
    exporter.observe([u.normalize_pod(pod())], at=NOW, now=NOW)
    with pytest.raises(ValueError, match="clock skew exceeds 60s"):
        exporter.observe([u.normalize_pod(pod())], at=NOW + timedelta(seconds=skew_seconds), now=NOW)
    assert not exporter.live
    assert len(rows(db)) == retained(db) == 1
    assert u._datetime(rows(db)[0]["observed_until"]) == NOW


def test_positive_skew_cannot_cross_database_week_or_extend_expiry(db):
    clock = datetime(2026, 11, 22, 15, 59, 59)
    replace_state(db, maintenance_at=clock.isoformat(), bytes_observed_at=clock.isoformat(), reconciled_at=clock.isoformat())
    exporter = u.MetadataExporter(db)
    exporter.observe([u.normalize_pod(pod())], at=clock + timedelta(seconds=30), now=clock)
    saved = rows(db)[0]
    assert str(saved["week_start"]) == "2026-11-16"
    assert u._datetime(saved["observed_from"]) == clock
    assert u._datetime(saved["expires_at"]) == datetime(2026, 11, 29, 16)
    later = clock + timedelta(seconds=2)
    exporter.observe([u.normalize_pod(pod())], at=later + timedelta(seconds=30), now=later)
    assert u._datetime(rows(db)[0]["observed_until"]) == datetime(2026, 11, 22, 16)
    assert u._datetime(rows(db)[1]["observed_from"]) == later


def test_expired_live_identity_is_discarded_before_new_baseline(db):
    exporter = u.MetadataExporter(db)
    expired = row(NOW - timedelta(days=15))
    exporter.live[expired["resource_id"]] = expired
    exporter.last_heartbeat = expired["observed_from"]
    exporter.observe([u.normalize_pod(pod())], at=NOW, now=NOW, heartbeat=True)
    assert len(rows(db)) == retained(db) == 1
    assert u._datetime(rows(db)[0]["observed_from"]) == NOW
    assert exporter.live[expired["resource_id"]]["expires_at"] == u.week_bounds(NOW)[2]


def test_database_clock_read_failure_drops_live_state(db, monkeypatch):
    exporter = u.MetadataExporter(db)
    exporter.observe([u.normalize_pod(pod())], at=NOW, now=NOW)
    def offline(*args, **kwargs):
        raise RuntimeError("DB clock offline")
    monkeypatch.setattr(u, "database_now", offline)
    with pytest.raises(RuntimeError, match="DB clock offline"):
        exporter.observe([], at=NOW, heartbeat=True)
    assert not exporter.live
    assert u._datetime(rows(db)[0]["observed_until"]) == NOW


def test_database_clock_override_is_offline_sqlite_only():
    class Connection:
        dialect = type("Dialect", (), {"name": "mysql"})()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, *args):
            pytest.fail("production clock override must fail before SQL")
    class Engine:
        def connect(self):
            return Connection()
    with pytest.raises(ValueError, match="offline SQLite"):
        u.database_now(Engine(), now=NOW)
    with pytest.raises(ValueError, match="offline SQLite"):
        u.save_observations(Engine(), [row()], now=NOW)


def _run_enabled_watch(monkeypatch, events, *, initial=None, client=None, stop=None, health_state=None):
    """Offline namespace loop, real normalization/exporter, injected lifecycle sink."""
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")
    stop = stop or threading.Event()
    lifecycle = []
    class Client:
        def list_collection(self, **kwargs):
            return initial or [], "rv"
        def stream_collection(self, **kwargs):
            assert kwargs["utilization_batches"] is True
            yield from events
            stop.set()
    def persist(engine, settings, snapshots, **kwargs):
        lifecycle.extend(snapshots)
        kwargs["on_lifecycle_rows"]([{"pod_uid": item.pod_uid} for item in snapshots])
        return len(snapshots)
    monkeypatch.setattr(watcher, "_persist_pod_snapshots", persist)
    class Recorder:
        def add(self, **kwargs):
            pass
    watcher._run_pod_worker(
        engine=None, settings=None, client=client or Client(),
        context=watcher.WatchRuntimeContext(u.SOURCE, u.CLUSTER, "ap-beijing", "TENCENT"),
        namespace_name="jenkins-tidb", stream_key="pods",
        health_state=health_state or watcher.WatchHealthState(stale_after_seconds=60), recorder=Recorder(),
        jenkins_prefix_cache=None, stop_event=stop,
    )
    return lifecycle


def test_postmortem_snapshots_never_insert_or_rewrite_an_ended_uid(db):
    exporter = u.MetadataExporter(db)
    record = u.normalize_pod(pod())
    # A UID first seen after termination has no confirmed-live interval to close.
    exporter.observe([record], at=NOW, now=NOW, deleted=True, heartbeat=True)
    assert not rows(db) and not exporter.live
    exporter.observe([record], at=NOW, now=NOW)
    ended = NOW + timedelta(minutes=5)
    exporter.observe([record], at=ended, now=ended, deleted=True, heartbeat=True)
    original = dict(rows(db)[0])
    for cycle in range(2, 5):
        clock = NOW + timedelta(seconds=300 * cycle)
        exporter.observe([record], at=clock, now=clock, deleted=True, heartbeat=True)
    assert [dict(saved) for saved in rows(db)] == [original]
    assert json.loads(original["attributes_json"])["ended_at"] == ended.isoformat()
    assert retained(db) == 1 and not exporter.live


@pytest.mark.parametrize("kind", ["DELETED", "Succeeded", "Failed"])
def test_clean_300_second_eofs_resume_last_rv_without_relist_or_postmortem_rows(db, monkeypatch, kind):
    clock = [NOW]
    saved = u.save_observations
    monkeypatch.setattr(u, "database_now", lambda *args, **kwargs: clock[0])
    monkeypatch.setattr(u, "utc_now", lambda: clock[0])
    monkeypatch.setattr(u, "save_observations", lambda engine, records: saved(db, records, now=clock[0]))
    stop = threading.Event()
    health = watcher.WatchHealthState(stale_after_seconds=720)
    monkeypatch.setattr(watcher.time, "monotonic", lambda: (clock[0] - NOW).total_seconds())
    ending = pod()
    ending["metadata"]["uid"] = "ending"
    terminal = copy.deepcopy(ending)
    terminal["status"] = {"phase": "Succeeded" if kind == "DELETED" else kind}
    postmortem = copy.deepcopy(terminal)
    postmortem["metadata"]["uid"] = "postmortem"
    def event(obj, rv):
        obj = copy.deepcopy(obj)
        obj["metadata"]["resourceVersion"] = rv
        return {"type": "DELETED" if kind == "DELETED" and obj["metadata"]["uid"] == "ending" else "MODIFIED", "object": obj}
    class Client:
        lists = 0
        watches = []
        def list_collection(self, **kwargs):
            self.lists += 1
            return [pod(), ending, postmortem], "list-rv"
        def stream_collection(self, **kwargs):
            self.watches.append(kwargs["resource_version"])
            cycle = len(self.watches)
            assert health.snapshot()["streams"]["pods"]["age_seconds"] == 0
            clock[0] = NOW + timedelta(seconds=300 * cycle)
            if cycle == 1:
                yield event(terminal, "end-rv")
                yield event(pod(), "event-rv")
            elif cycle == 2:
                yield event(terminal, "linger-rv")
                yield event(postmortem, "postmortem-rv")
                yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "bookmark-rv"}}}
            # Cycle 3 is idle: a clean EOF must still confirm the live set.
            elif cycle == 4:
                yield event(terminal, "linger-again-rv")
                stop.set()
    client = Client()
    _run_enabled_watch(monkeypatch, [], client=client, stop=stop, health_state=health)
    assert client.lists == 1
    assert client.watches == ["list-rv", "event-rv", "bookmark-rv", "bookmark-rv"]
    saved_rows = {item["resource_id"]: item for item in rows(db)}
    assert len(saved_rows) == retained(db) == 2
    assert u._datetime(saved_rows["pod-uid"]["observed_from"]) == NOW
    assert u._datetime(saved_rows["pod-uid"]["observed_until"]) == clock[0]
    assert u._datetime(saved_rows["ending"]["observed_until"]) == NOW + timedelta(seconds=300)
    assert json.loads(saved_rows["ending"]["attributes_json"])["ended_at"] == (NOW + timedelta(seconds=300)).isoformat()


def test_read_burst_coalescing_is_bounded_even_for_repeated_uid(monkeypatch):
    batches = []
    monkeypatch.setattr(watcher, "_export_utilization", lambda exporter, records, **kwargs: batches.append(copy.deepcopy(records)))
    def events():
        for number in range(1001):
            obj = pod()
            obj["spec"]["nodeName"] = f"node-{number}"
            yield {"type": "MODIFIED", "object": obj}
            if number == 999:
                assert len(batches) == 1  # 1000 inputs, even though only one UID.
        yield {"type": watcher.UTILIZATION_BATCH_END}
        assert len(batches) == 2  # Flush before the stream asks for more data.
    assert len(_run_enabled_watch(monkeypatch, events())) == 1001
    assert [len(batch) for batch in batches] == [1, 1]
    assert [batch[0]["attributes_json"]["node_name"] for batch in batches] == ["node-999", "node-1000"]


def test_batch_has_at_most_1000_distinct_records_and_flushes_on_stop(monkeypatch):
    batches = []
    monkeypatch.setattr(watcher, "_export_utilization", lambda exporter, records, **kwargs: batches.append(records))
    def events():
        for number in range(1001):
            obj = pod()
            obj["metadata"]["uid"] = f"pod-{number}"
            yield {"type": "ADDED", "object": obj}
    _run_enabled_watch(monkeypatch, events())
    assert [len(batch) for batch in batches] == [1000, 1]


def test_event_processing_deadline_flushes_before_next_event(monkeypatch):
    batches = []
    clock = [0.0]
    monkeypatch.setattr(watcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(watcher, "_export_utilization", lambda exporter, records, **kwargs: batches.append(records))
    def events():
        yield {"type": "ADDED", "object": pod()}
        clock[0] = 1.0
        yield {"type": "MODIFIED", "object": pod()}
        assert len(batches) == 1
    _run_enabled_watch(monkeypatch, events())
    assert [len(batch) for batch in batches] == [1, 1]


@pytest.mark.parametrize("kind", ["DELETED", "Succeeded", "Failed"])
def test_terminal_record_flushes_pending_live_before_closure(monkeypatch, kind):
    batches = []
    monkeypatch.setattr(watcher, "_export_utilization", lambda exporter, records, **kwargs: batches.append((copy.deepcopy(records), kwargs)))
    ended = pod()
    if kind != "DELETED":
        ended["status"] = {"phase": kind}
    events = [{"type": "ADDED", "object": pod()},
              {"type": "DELETED" if kind == "DELETED" else "MODIFIED", "object": ended}]
    _run_enabled_watch(monkeypatch, events)
    assert len(batches) == 2
    assert not batches[0][1].get("deleted")
    assert batches[1][1]["deleted"] is True


def test_dropped_burst_is_not_retried_or_bridged_on_recovery(db, monkeypatch, caplog):
    clock = [NOW]
    saved = u.save_observations
    calls = []
    def save(engine, records):
        calls.append(copy.deepcopy(records))
        if len(calls) == 2:
            raise ValueError("guard paused during outage")
        return saved(db, records, now=clock[0])
    monkeypatch.setattr(u, "database_now", lambda *args, **kwargs: clock[0])
    monkeypatch.setattr(u, "utc_now", lambda: clock[0])
    monkeypatch.setattr(u, "save_observations", save)
    def events():
        clock[0] = NOW + timedelta(seconds=1)
        yield {"type": "MODIFIED", "object": pod()}
        yield {"type": watcher.UTILIZATION_BATCH_END}
        assert len(calls) == 2
        assert len(rows(db)) == 1
        clock[0] = NOW + timedelta(minutes=2)
        yield {"type": "MODIFIED", "object": pod()}
        yield {"type": watcher.UTILIZATION_BATCH_END}
    lifecycle = _run_enabled_watch(monkeypatch, events(), initial=[pod()])
    assert len(lifecycle) == 3  # Metadata outage never breaks lifecycle persistence.
    assert "evidence dropped" in caplog.text and "guard paused during outage" in caplog.text
    assert len(calls) == 3  # No failed-batch replay on stop.
    assert len(rows(db)) == retained(db) == 2
    assert u._datetime(rows(db)[0]["observed_until"]) == NOW
    assert u._datetime(rows(db)[1]["observed_from"]) == NOW + timedelta(minutes=2)


@pytest.mark.parametrize("failure", ["gap", "watch_410", "http_410", "exception"])
def test_watch_gap_flushes_then_relists_without_backdating(db, monkeypatch, failure):
    clock = [NOW]
    saved = u.save_observations
    monkeypatch.setattr(u, "database_now", lambda *args, **kwargs: clock[0])
    monkeypatch.setattr(u, "utc_now", lambda: clock[0])
    monkeypatch.setattr(u, "save_observations", lambda engine, records: saved(db, records, now=clock[0]))
    monkeypatch.setattr(watcher, "_sleep_until_retry", lambda stop: None)
    stop = threading.Event()
    class Client:
        lists = 0
        watches = []
        def list_collection(self, **kwargs):
            self.lists += 1
            if self.lists == 2:
                clock[0] = NOW + timedelta(minutes=2)
            return [pod()], f"list-{self.lists}"
        def stream_collection(self, **kwargs):
            self.watches.append(kwargs["resource_version"])
            if self.lists == 2:
                stop.set()
                return
            clock[0] = NOW + timedelta(seconds=1)
            yield {"type": "MODIFIED", "object": pod()}
            if failure == "gap":
                yield {"type": watcher.UTILIZATION_GAP}
            elif failure == "watch_410":
                yield {"type": "ERROR", "object": {"code": 410, "reason": "Expired"}}
            elif failure == "http_410":
                raise watcher.urllib_error.HTTPError("https://offline.test", 410, "expired", {}, None)
            else:
                raise RuntimeError("offline disconnect")
            pytest.fail("a gap must relist before consuming any more WATCH events")
    client = Client()
    _run_enabled_watch(monkeypatch, [], client=client, stop=stop)
    assert client.lists == 2 and client.watches == ["list-1", "list-2"]
    assert len(rows(db)) == retained(db) == 2
    assert u._datetime(rows(db)[0]["observed_until"]) == NOW + timedelta(seconds=1)
    assert u._datetime(rows(db)[1]["observed_from"]) == NOW + timedelta(minutes=2)


@pytest.mark.parametrize("http_error", [False, True])
def test_enabled_401_relists_before_reconnecting_a_watch(db, monkeypatch, http_error):
    clock = [NOW]
    saved = u.save_observations
    monkeypatch.setattr(u, "database_now", lambda *args, **kwargs: clock[0])
    monkeypatch.setattr(u, "utc_now", lambda: clock[0])
    monkeypatch.setattr(u, "save_observations", lambda engine, records: saved(db, records, now=clock[0]))
    stop = threading.Event()
    calls = []
    def get(url, **kwargs):
        lists = sum(kind == "list" for kind, _ in calls) + 1
        calls.append(("list", url))
        if lists == 2:
            clock[0] = NOW + timedelta(minutes=2)
        return {"items": [pod()], "metadata": {"resourceVersion": f"list-{lists}"}}
    def stream(url, **kwargs):
        calls.append(("watch", url))
        if "resourceVersion=list-2" in url:
            stop.set()
            return
        assert "resourceVersion=list-1" in url
        clock[0] = NOW + timedelta(seconds=1)
        obj = pod()
        obj["metadata"]["resourceVersion"] = "event-rv"
        yield {"type": "MODIFIED", "object": obj}
        if http_error:
            cause = watcher.urllib_error.HTTPError(url, 401, "expired token", {}, None)
            raise RuntimeError("Kubernetes watch failed: HTTP 401") from cause
        yield {"type": "ERROR", "object": {"code": 401}}
    monkeypatch.setattr(watcher, "_get_json", get)
    monkeypatch.setattr(watcher, "_stream_watch_json", stream)
    client = watcher._KubernetesClient(base_url="https://offline.test", token="offline", ca_file=None,
                                       watch_timeout_seconds=300)
    _run_enabled_watch(monkeypatch, [], client=client, stop=stop)
    assert [kind for kind, _ in calls] == ["list", "watch", "list", "watch"]
    assert len(rows(db)) == retained(db) == 2
    assert u._datetime(rows(db)[0]["observed_until"]) == NOW + timedelta(seconds=1)
    assert u._datetime(rows(db)[1]["observed_from"]) == NOW + timedelta(minutes=2)


@pytest.mark.parametrize("failure_stage", ["event_flush", "bookmark", "eof_heartbeat"])
def test_export_failure_forces_relist_instead_of_resuming_unconfirmed_uids(db, monkeypatch, failure_stage):
    clock = [NOW]
    saved = u.save_observations
    calls = []
    def save(engine, records):
        calls.append(copy.deepcopy(records))
        if len(calls) == 2:
            raise RuntimeError("offline metadata sink")
        return saved(db, records, now=clock[0])
    monkeypatch.setattr(u, "database_now", lambda *args, **kwargs: clock[0])
    monkeypatch.setattr(u, "utc_now", lambda: clock[0])
    monkeypatch.setattr(u, "save_observations", save)
    stop = threading.Event()
    class Client:
        lists = 0
        def list_collection(self, **kwargs):
            self.lists += 1
            if self.lists == 2:
                clock[0] = NOW + timedelta(minutes=10)
            return [pod()], f"list-{self.lists}"
        def stream_collection(self, **kwargs):
            if self.lists == 2:
                assert kwargs["resource_version"] == "list-2"
                stop.set()
                return
            clock[0] = NOW + timedelta(minutes=5)
            if failure_stage == "event_flush":
                yield {"type": "MODIFIED", "object": pod()}
            elif failure_stage == "bookmark":
                yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "next"}}}
    client = Client()
    _run_enabled_watch(monkeypatch, [], client=client, stop=stop)
    assert client.lists == 2 and len(calls) == 3  # Failed evidence is not replayed.
    assert len(rows(db)) == retained(db) == 2
    assert u._datetime(rows(db)[0]["observed_until"]) == NOW
    assert u._datetime(rows(db)[1]["observed_from"]) == NOW + timedelta(minutes=10)


def test_watch_without_resource_version_relists_instead_of_claiming_continuity(db, monkeypatch):
    clock = [NOW]
    saved = u.save_observations
    monkeypatch.setattr(u, "database_now", lambda *args, **kwargs: clock[0])
    monkeypatch.setattr(u, "utc_now", lambda: clock[0])
    monkeypatch.setattr(u, "save_observations", lambda engine, records: saved(db, records, now=clock[0]))
    stop = threading.Event()
    class Client:
        lists = 0
        def list_collection(self, **kwargs):
            self.lists += 1
            clock[0] = NOW + timedelta(seconds=300 * (self.lists - 1))
            return [pod()], None
        def stream_collection(self, **kwargs):
            assert kwargs["resource_version"] is None
            if self.lists == 2:
                stop.set()
            yield from []
    client = Client()
    _run_enabled_watch(monkeypatch, [], client=client, stop=stop)
    assert client.lists == 2 and len(rows(db)) == retained(db) == 2
    assert u._datetime(rows(db)[0]["observed_until"]) == NOW
    assert u._datetime(rows(db)[1]["observed_from"]) == NOW + timedelta(seconds=300)


def test_watch_exception_flushes_before_retry_wait(monkeypatch):
    batches = []
    monkeypatch.setattr(watcher, "_export_utilization", lambda exporter, records, **kwargs: batches.append(records))
    def wait(stop):
        assert [len(batch) for batch in batches] == [1]
        stop.set()
    monkeypatch.setattr(watcher, "_sleep_until_retry", wait)
    def events():
        yield {"type": "ADDED", "object": pod()}
        raise RuntimeError("offline watch disconnected")
    _run_enabled_watch(monkeypatch, events())
    assert len(batches) == 1


def test_sink_still_rejects_future_evidence_without_identity_clamping(db):
    future = row(NOW + timedelta(microseconds=1))
    with pytest.raises(ValueError, match="invalid or unbounded"):
        u.save_observations(db, [future], now=NOW)
    assert not rows(db) and retained(db) == 0


def test_commit_clock_expiry_fence_rolls_back_entire_metadata_batch(db):
    before = datetime(2026, 11, 29, 15, 59, 59)
    expiry = datetime(2026, 11, 29, 16)
    replace_state(db, maintenance_at=before.isoformat(), bytes_observed_at=before.isoformat(), reconciled_at=before.isoformat())
    clocks = iter([before.isoformat(), expiry.isoformat()])
    with db.connect() as c:
        c.connection.driver_connection.create_function("current_timestamp", 0, lambda: next(clocks))
    with pytest.raises(ValueError, match="week expired"):
        u.save_observations(db, [row(datetime(2026, 11, 22, 15, 59))])
    assert not rows(db) and retained(db) == 0


def test_partial_committed_flush_failure_discards_all_in_memory_continuity(db, monkeypatch):
    saved = u.save_observations
    calls = []
    def save(engine, records, **kwargs):
        calls.append(len(records))
        if len(calls) == 2:
            raise RuntimeError("second transaction offline")
        return saved(engine, records, **kwargs)
    monkeypatch.setattr(u, "save_observations", save)
    records = []
    for number in range(1001):
        record = u.normalize_pod(pod())
        record["resource_id"] = f"pod-{number}"
        records.append(record)
    exporter = u.MetadataExporter(db)
    with pytest.raises(RuntimeError, match="second transaction"):
        exporter.observe(records, at=NOW, now=NOW, heartbeat=True)
    assert calls == [1000, 1]
    assert not exporter.live and exporter.last_heartbeat is None
    assert len(rows(db)) == retained(db) == 1000
    later = NOW + timedelta(minutes=2)
    exporter.observe([records[0]], at=later, now=later)
    assert calls == [1000, 1, 1]  # Failed work is not replayed.
    matching = [value for value in rows(db) if value["resource_id"] == "pod-0"]
    assert len(matching) == 2
    assert u._datetime(matching[0]["observed_until"]) == NOW
    assert u._datetime(matching[1]["observed_from"]) == later
