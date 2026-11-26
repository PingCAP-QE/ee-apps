from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text

from ci_dashboard.jobs import pod_watcher as watcher
from ci_dashboard.jobs import utilization_metadata as u

NOW = datetime(2026, 11, 24, 4)


def pod():
    return {"metadata": {"uid": "pod-uid", "namespace": "jenkins-tidb", "name": "random-pod-3290",
                         "creationTimestamp": "2026-11-01T00:00:00Z",
                         "labels": {"org": "pingcap", "repo": "tidb", "author": "private-person", "owner": "private"},
                         "annotations": {"buildUrl": "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_mysql_test/3290/", "email": "private@example.com"}},
            "spec": {"nodeName": "node-a", "containers": [{"resources": {"requests": {"cpu": "2100m", "memory": "2Gi"}, "limits": {"cpu": "4", "memory": "4Gi"}}}]}}


def node(name="node-a", provider="qcloud:///ap-beijing/np-123"):
    return {"metadata": {"uid": "node-uid", "name": name, "creationTimestamp": "2026-10-01T00:00:00Z",
                         "labels": {"owner": "private"}},
            "spec": {"providerID": provider}, "status": {"capacity": {"cpu": "8"}}}


def context(at=NOW, **changes):
    record = u.normalize_pod(pod())
    return {**record, "context_key": u.context_key(record, at), "observed_from": at,
            "observed_until": at, "planned_until": at, "closed_at": None,
            "expires_at": at + u.CONTEXT_TTL, **changes}


def state(**changes):
    return {"version": 2, "enabled": True, "paused": False, "pause_reasons": [], "counts_reliable": True,
            "contracts": {"pod": {}}, "retained_rows": 0, "observed_bytes": 0,
            "maintenance_at": NOW.isoformat(), "bytes_observed_at": NOW.isoformat(),
            "scope": {"account_id": u.ACCOUNT, "cluster_id": u.CLUSTER, "cluster_region": "ap-beijing",
                      "regions": ["ap-beijing", "ap-hongkong"], "namespaces": ["jenkins-tidb"],
                      "labels": sorted(u.LABELS)}, **changes}


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE cost_job_state (job_name TEXT PRIMARY KEY, watermark_json TEXT)")
        # Literal V2 schema: CI Dashboard must not import the independent Cost Insight package.
        c.exec_driver_sql("""CREATE TABLE pod_utilization_context (
            id INTEGER PRIMARY KEY, context_key TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL,
            cluster_id TEXT NOT NULL, namespace TEXT NOT NULL, pod_uid TEXT NOT NULL, pod_name TEXT NOT NULL,
            node_query_kind TEXT NOT NULL, node_query_value TEXT NOT NULL,
            cpu_request_cores FLOAT, memory_request_bytes FLOAT, labels_json TEXT NOT NULL,
            observed_from DATETIME NOT NULL, observed_until DATETIME NOT NULL, closed_at DATETIME,
            planned_until DATETIME NOT NULL, expires_at DATETIME NOT NULL)""")
        c.execute(text("INSERT INTO cost_job_state VALUES (:key,:state)"), {"key": u.GUARD_KEY, "state": json.dumps(state())})
    yield engine
    engine.dispose()


def replace_state(engine, **changes):
    with engine.begin() as c:
        c.execute(text("UPDATE cost_job_state SET watermark_json=:state"), {"state": json.dumps(state(**changes))})


def rows(engine):
    with engine.connect() as c:
        return list(c.execute(text("SELECT * FROM pod_utilization_context ORDER BY observed_from,id")).mappings())


def retained(engine):
    with engine.connect() as c:
        return json.loads(c.execute(text("SELECT watermark_json FROM cost_job_state")).scalar_one())["retained_rows"]


def observe(exporter, records, at=NOW, **kwargs):
    exporter.observe(records, at=at, now=at, **kwargs)


def pod_evidence(obj, seen_at, **kwargs):
    return {**u.normalize_pod(obj, **kwargs), "_node_ref": u.pod_node_ref(obj, seen_at)}


def test_custom_label_and_real_native_selector_are_preserved(db, monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_LABEL_KEYS", "test_campaign")
    obj = pod()
    obj["metadata"]["labels"]["test_campaign"] = "compatibility"
    cache = u.NodeSelectorCache()
    listed_at = u.time.monotonic()
    cache.refresh([node(provider="qcloud:///ap-beijing/np-244kqxxd-j5swq")], listed_at=listed_at)
    exporter = u.MetadataExporter(db, node_cache=cache)
    record = pod_evidence(obj, listed_at - 1, selected_labels=exporter.selected_labels)
    assert record["labels_json"] == {"test_campaign": "compatibility"}
    scope = state()["scope"]
    scope["labels"] = ["test_campaign"]
    replace_state(db, scope=scope)
    observe(exporter, [record])
    saved = rows(db)[0]
    assert json.loads(saved["labels_json"]) == {"test_campaign": "compatibility"}
    assert saved["node_query_value"] == "np-244kqxxd-j5swq"


def test_label_configuration_is_validated_once_per_exporter(monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_LABEL_KEYS", "test_campaign")
    validate = Mock(wraps=u.label_keys)
    monkeypatch.setattr(u, "label_keys", validate)
    exporter = u.MetadataExporter(None)
    validate.assert_called_once_with(["test_campaign"])
    # Runtime changes cannot make normalization raise in the shared watch loop.
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_LABEL_KEYS", "repo, job")
    obj = pod()
    obj["metadata"]["labels"]["test_campaign"] = "compatibility"
    for _ in range(2):
        assert u.normalize_pod(obj, selected_labels=exporter.selected_labels)["labels_json"] == {
            "test_campaign": "compatibility"}
    assert validate.call_count == 1


def test_opt_in_only_exact_source(monkeypatch):
    monkeypatch.delenv("CI_DASHBOARD_UTILIZATION_ENABLED", raising=False)
    assert not u.enabled(u.SOURCE)
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_ENABLED", "true")
    assert u.enabled(u.SOURCE) and not u.enabled("gcp:other")


@pytest.mark.parametrize(("raw", "expected"), [("100m", .1), ("1Gi", 1024**3), ("1e3", 1000), ("2M", 2_000_000), ("1u", 1e-6), ("1n", 1e-9), ("2Ki", 2048)])
def test_quantity(raw, expected):
    assert u.quantity(raw) == Decimal(str(expected))


@pytest.mark.parametrize("raw", ["NaN", "-1", "Infinity", "garbage", "1GiB", "1e9999999"])
def test_invalid_quantity(raw):
    with pytest.raises(ValueError):
        u.quantity(raw)


def test_scheduler_requests_init_sidecar_overhead_defaulting_and_zero():
    spec = {"containers": [{"resources": {"requests": {"cpu": "2", "memory": "1Gi"}}}],
            "initContainers": [{"restartPolicy": "Always", "resources": {"requests": {"cpu": "500m", "memory": "128Mi"}}},
                               {"resources": {"requests": {"cpu": "4", "memory": "2Gi"}}}],
            "overhead": {"cpu": "100m", "memory": "64Mi"}}
    assert u.effective_resources(spec) == {"cpu_request_cores": 4.6, "memory_request_bytes": 2240 * 1024**2}
    spec["initContainers"].reverse()
    assert u.effective_resources(spec)["cpu_request_cores"] == 4.1
    assert u.effective_resources({"containers": [{"resources": {"limits": {"cpu": "0.0001", "memory": "0.1"}}}]}) == {
        "cpu_request_cores": .001, "memory_request_bytes": 1}
    assert u.effective_resources({"containers": [{}]}) == {"cpu_request_cores": 0, "memory_request_bytes": 0}


def test_cpu_requests_are_summed_without_floating_point_roundup():
    spec = {"containers": [{"resources": {"requests": {"cpu": value}}} for value in ("100m", "200m")]}
    assert u.effective_resources(spec)["cpu_request_cores"] == 0.300


@pytest.mark.parametrize("kind", ["pod_level", "resize", "status_resources", "bad_quantity", "missing_containers"])
def test_requests_unknown_not_zero(kind):
    value = pod()
    if kind == "pod_level":
        value["spec"]["resources"] = {"requests": {"cpu": "8"}}
    elif kind == "resize":
        value["status"] = {"resize": "InProgress"}
    elif kind == "status_resources":
        value["status"] = {"containerStatuses": [{"allocatedResources": {"cpu": "1"}}]}
    elif kind == "missing_containers":
        value["spec"].pop("containers")
    else:
        value["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "bad"
    record = u.normalize_pod(value)
    assert record["cpu_request_cores"] is record["memory_request_bytes"] is None


def test_generic_labels_and_linkage_no_limits_person_or_raw_objects():
    value = pod()
    value["metadata"]["labels"].update({"service": "ci", "project": "db", "environment": "test", "test_project": "tidb", "test_suite": "mysql"})
    record = u.normalize_pod(value, linkage={"repo_full_name": "pingcap/tidb", "pod_author": "secret"})
    assert set(record) == set(u.POD_FIELDS)
    assert record["labels_json"]["job"] == "/jenkins/job/pingcap/job/tidb/job/ghpr_mysql_test/"
    assert record["labels_json"]["repo"] == "pingcap/tidb"
    assert set(record["labels_json"]) == u.LABELS
    assert "private" not in json.dumps(record) and "secret" not in json.dumps(record)
    value["metadata"]["labels"] = {"service": "database"}
    value["metadata"]["annotations"] = {}
    assert u.normalize_pod(value)["labels_json"] == {"service": "database"}
    value["metadata"]["labels"] = {"prow.k8s.io/refs.org": "pingcap", "prow.k8s.io/refs.repo": "tidb"}
    assert u.normalize_pod(value)["labels_json"] == {"repo": "pingcap/tidb"}
    assert u.normalize_pod({}) is None


@pytest.mark.parametrize("url", [None, "random-pod-3290", "https://prow.tidb.net/view/gs/logs/random/123/",
                                 "https://do.pingcap.net/jenkins/job/repo/job/PR-123/3290/"])
def test_unstable_job_omitted(url):
    assert u.stable_job(url) == ""


def test_without_stable_jenkins_evidence_raw_job_does_not_become_a_dimension():
    obj = pod()
    obj["metadata"]["annotations"] = {}
    obj["metadata"]["labels"]["job"] = "build-aa08f14"
    assert u.normalize_pod(obj)["labels_json"] == {"repo": "pingcap/tidb"}


@pytest.mark.parametrize(("node_created", "expected"), [
    ("2026-11-24T03:30:00Z", "np-123"),  # Autoscaler added the node while the Pod was Pending.
    ("2026-11-24T03:59:30Z", ""),        # A replacement at the same name after scheduling.
])
def test_pod_scheduled_evidence_disambiguates_nodes_created_after_pod(db, node_created, expected):
    obj = pod()
    obj["status"] = {"conditions": [{"type": "PodScheduled", "status": "True",
                                    "lastTransitionTime": "2026-11-24T03:59:00Z"}]}
    target = node()
    target["metadata"]["creationTimestamp"] = node_created
    listed_at = u.time.monotonic()
    cache = u.NodeSelectorCache()
    cache.refresh([target], listed_at=listed_at)
    exporter = u.MetadataExporter(db, node_cache=cache)
    observe(exporter, [pod_evidence(obj, listed_at - 1)])
    assert rows(db)[0]["node_query_value"] == expected


def test_node_selector_cache_bounded_expiring_and_provider_preferred(monkeypatch):
    clock = [0]
    monkeypatch.setattr(u.time, "monotonic", lambda: clock[0])
    cache = u.NodeSelectorCache(max_nodes=2, ttl_seconds=10)
    assert cache.snapshot()[1] == {}
    cache.refresh([node(), node("10.0.6.207", "ins-abc")], listed_at=0)
    _, nodes = cache.snapshot()
    assert nodes["node-a"].instance_id == "np-123"
    assert nodes["10.0.6.207"].instance_id == "ins-abc"
    clock[0] = 11
    assert cache.snapshot()[1] == {}
    cache.refresh([node("10.0.6.207", "qcloud:///ap-hongkong/ins-abc")], listed_at=11)
    assert cache.snapshot()[1] == {}  # No IP fallback, even with a listed Node.
    with pytest.raises(ValueError, match="bound"):
        cache.refresh([node()] * 3, listed_at=11)
    assert cache.snapshot()[1] == {}
    assert u.normalize_node({}) is None


def test_stable_change_enrichment_heartbeat_and_ended_tail(db):
    cache = u.NodeSelectorCache()
    exporter = u.MetadataExporter(db, node_cache=cache)
    record = pod_evidence(pod(), u.time.monotonic() - 1)
    observe(exporter, [record])
    observe(exporter, [record], NOW + timedelta(minutes=1))
    assert len(rows(db)) == retained(db) == 1
    assert u._datetime(rows(db)[0]["observed_from"]) == NOW  # Not creationTimestamp.
    cache.refresh([node()], listed_at=u.time.monotonic())
    enriched = pod_evidence(pod(), u.time.monotonic())
    observe(exporter, [enriched], NOW + timedelta(minutes=2), heartbeat=True)
    old, current = rows(db)
    assert old["node_query_kind"] == "" and current["node_query_value"] == "np-123"
    assert u._datetime(old["closed_at"]) == NOW + timedelta(minutes=2)
    assert u._datetime(current["observed_from"]) == NOW + timedelta(minutes=2)
    # Dispatcher progress must survive every watcher renewal/closure.
    with db.begin() as c:
        c.execute(text("UPDATE pod_utilization_context SET planned_until=:at WHERE id=:id"),
                  {"at": NOW + timedelta(minutes=2, seconds=30), "id": current["id"]})
    observe(exporter, [], NOW + timedelta(minutes=3), heartbeat=True)
    observe(exporter, [enriched], NOW + timedelta(minutes=5), deleted=True, heartbeat=True)
    ended = rows(db)[1]
    assert u._datetime(ended["observed_until"]) == u._datetime(ended["closed_at"]) == NOW + timedelta(minutes=3)
    assert u._datetime(ended["planned_until"]) == NOW + timedelta(minutes=2, seconds=30)
    assert u._datetime(ended["expires_at"]) == NOW + timedelta(minutes=3, hours=24)
    original = dict(ended)
    observe(exporter, [enriched], NOW + timedelta(minutes=6), deleted=True, heartbeat=True)
    assert dict(rows(db)[1]) == original and not exporter.live
    assert retained(db) == 2


@pytest.mark.parametrize(("gap_minutes", "confirm_unbound"), [(0, False), (5, False), (5, True)])
def test_selector_loss_and_metadata_change_keep_old_metadata_until_observed(db, gap_minutes, confirm_unbound):
    cache = u.NodeSelectorCache()
    listed_at = u.time.monotonic()
    cache.refresh([node()], listed_at=listed_at)
    exporter = u.MetadataExporter(db, node_cache=cache)
    obj = pod()
    obj["metadata"]["labels"]["service"] = "first"
    observe(exporter, [pod_evidence(obj, listed_at - 1)])
    last_bound = NOW + timedelta(minutes=1)
    observe(exporter, [], last_bound, heartbeat=True)
    with db.begin() as c:
        c.execute(text("UPDATE pod_utilization_context SET planned_until=:at"), {"at": last_bound})

    cache.clear()
    if confirm_unbound:
        observe(exporter, [], last_bound + timedelta(minutes=2), heartbeat=True)
    obj["metadata"]["labels"]["service"] = "second"
    obj["spec"]["containers"][0]["resources"]["requests"].update(cpu="4", memory="4Gi")
    changed = last_bound + timedelta(minutes=gap_minutes)
    observe(exporter, [pod_evidence(obj, listed_at + 1)], changed, heartbeat=True)
    # A fresh Node LIST can bind the Pod again, but not the unresolved history.
    rebound = changed + timedelta(minutes=1)
    cache.refresh([node()], listed_at=listed_at + 2)
    observe(exporter, [], rebound, heartbeat=True)
    end = rebound + timedelta(minutes=1)
    observe(exporter, [], end, heartbeat=True)

    saved = rows(db)
    expected = [(NOW, last_bound, "un_instance_id", "np-123", "first", 2.1, 2 * 1024**3)]
    if gap_minutes:
        expected.append((last_bound, changed, "", "", "first", 2.1, 2 * 1024**3))
    expected += [(changed, rebound, "", "", "second", 4.0, 4 * 1024**3),
                 (rebound, end, "un_instance_id", "np-123", "second", 4.0, 4 * 1024**3)]
    assert [(u._datetime(row["observed_from"]), u._datetime(row["observed_until"]),
             row["node_query_kind"], row["node_query_value"], json.loads(row["labels_json"])["service"],
             row["cpu_request_cores"], row["memory_request_bytes"]) for row in saved] == expected
    assert len(saved) == retained(db)
    assert u._datetime(saved[0]["planned_until"]) == last_bound
    for row, (start, until, *_) in zip(saved, expected, strict=True):
        assert row["context_key"] == u.context_key(row, start)
        assert u._datetime(row["expires_at"]) == until + u.CONTEXT_TTL
        closed_at = row["closed_at"]
        assert (u._datetime(closed_at) if closed_at else None) == (None if start == rebound else until)
        if start != NOW:
            assert u._datetime(row["planned_until"]) == start


def test_selector_loss_without_room_for_a_gap_does_not_collide_with_initial_context(db):
    cache = u.NodeSelectorCache()
    listed_at = u.time.monotonic()
    cache.refresh([node()], listed_at=listed_at)
    exporter = u.MetadataExporter(db, node_cache=cache)
    obj = pod()
    observe(exporter, [pod_evidence(obj, listed_at - 1)])
    cache.clear()
    obj["metadata"]["labels"]["service"] = "second"
    changed = pod_evidence(obj, listed_at + 1)
    observe(exporter, [changed])  # The original start already owns this DB timestamp.
    next_tick = NOW + timedelta(microseconds=1)
    observe(exporter, [changed], next_tick)
    old, new = rows(db)
    assert len(rows(db)) == retained(db) == 2
    assert u._datetime(old["closed_at"]) == NOW
    assert u._datetime(new["observed_from"]) == u._datetime(new["observed_until"]) == next_tick
    assert new["context_key"] == u.context_key(new, next_tick)
    assert new["node_query_value"] == "" and json.loads(new["labels_json"])["service"] == "second"


def test_no_week_slicing_and_stale_heartbeat_does_not_bridge(db):
    boundary = datetime(2026, 11, 22, 16)
    first = boundary - timedelta(minutes=1)
    replace_state(db, maintenance_at=first.isoformat(), bytes_observed_at=first.isoformat())
    exporter = u.MetadataExporter(db)
    record = u.normalize_pod(pod())
    observe(exporter, [record], first)
    observe(exporter, [], boundary + timedelta(minutes=1), heartbeat=True)
    assert len(rows(db)) == 1
    observe(exporter, [record], boundary + timedelta(minutes=16), heartbeat=True)
    old, new = rows(db)
    assert u._datetime(old["closed_at"]) == boundary + timedelta(minutes=1)
    assert u._datetime(new["observed_from"]) == boundary + timedelta(minutes=16)


def test_reset_closes_last_evidence_and_relist_never_backdates(db, monkeypatch):
    clock = [NOW]
    real_save = u.save_contexts
    monkeypatch.setattr(u, "save_contexts", lambda engine, contexts, **kwargs: real_save(db, contexts, now=clock[0]))
    exporter = u.MetadataExporter(db)
    record = u.normalize_pod(pod())
    observe(exporter, [record])
    clock[0] += timedelta(minutes=2)
    exporter.reset()
    assert u._datetime(rows(db)[0]["closed_at"]) == NOW
    observe(exporter, [record], clock[0])
    assert len(rows(db)) == retained(db) == 2
    assert u._datetime(rows(db)[1]["observed_from"]) == clock[0]


def test_terminal_only_does_not_invent_context(db):
    exporter = u.MetadataExporter(db)
    observe(exporter, [u.normalize_pod(pod())], deleted=True, heartbeat=True)
    assert not rows(db) and not exporter.live


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"paused": True}, {"counts_reliable": False}, {"version": 1},
    {"maintenance_at": None}, {"maintenance_at": (NOW - timedelta(hours=4, seconds=1)).isoformat()},
    {"maintenance_at": (NOW + timedelta(seconds=61)).isoformat()},
    {"bytes_observed_at": (NOW - timedelta(hours=5)).isoformat()}, {"observed_bytes": None},
    {"observed_bytes": float("nan")}, {"observed_bytes": 8 * 1024**3},
    {"retained_rows": 1_999_999}, {"retained_rows": -1}, {"retained_rows": True}, {"scope": {}},
])
def test_guard_fail_closed_and_atomic_caps(db, changes):
    replace_state(db, **changes)
    with pytest.raises(ValueError):
        u.save_contexts(db, [context()], now=NOW)
    assert not rows(db)


def test_namespace_and_labels_guard_scope(db):
    scope = state()["scope"]
    scope["namespaces"] = ["other"]
    replace_state(db, scope=scope)
    with pytest.raises(ValueError, match="scope"):
        u.save_contexts(db, [context()], now=NOW)
    scope["namespaces"] = ["jenkins-tidb"]
    scope["labels"] = ["service"]
    replace_state(db, scope=scope)
    with pytest.raises(ValueError, match="scope"):
        u.save_contexts(db, [context()], now=NOW)


def test_missing_guard_and_rollback_counter(db):
    with db.begin() as c:
        c.exec_driver_sql("DELETE FROM cost_job_state")
    with pytest.raises(ValueError):
        u.save_contexts(db, [context()], now=NOW)
    replace_state(db)
    with db.begin() as c:
        c.execute(text("INSERT INTO cost_job_state VALUES (:key,:state)"), {"key": u.GUARD_KEY, "state": json.dumps(state())})
        c.exec_driver_sql("CREATE TRIGGER reject_guard_update BEFORE UPDATE ON cost_job_state BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
    with pytest.raises(Exception, match="offline failure"):
        u.save_contexts(db, [context()], now=NOW)
    assert not rows(db) and retained(db) == 0


@pytest.mark.parametrize("changes", [
    {"cpu_request_cores": -1}, {"memory_request_bytes": float("inf")}, {"pod_uid": "x" * 256},
    {"labels_json": {"author": "person"}}, {"labels_json": {"service": "x" * 513}},
    {"node_query_kind": "node", "node_query_value": "node-a"}, {"context_key": "x" * 64},
    {"observed_until": NOW + timedelta(seconds=1)}, {"closed_at": NOW - timedelta(seconds=1)},
    {"expires_at": NOW + timedelta(days=14)}, {"cpu_limit": 4},
])
def test_sink_rejects_invalid_context(db, changes):
    with pytest.raises(ValueError):
        u.save_contexts(db, [context(**changes)], now=NOW)
    assert not rows(db) and retained(db) == 0


def test_closed_context_immutable_and_stale_writer_rejected(db):
    original = context()
    u.save_contexts(db, [original], now=NOW)
    changed = {**original, "cpu_request_cores": 5}
    with pytest.raises(ValueError, match="conflicting"):
        u.save_contexts(db, [changed], now=NOW)
    closed = {**original, "closed_at": NOW}
    u.save_contexts(db, [closed], now=NOW)
    u.save_contexts(db, [closed], now=NOW)  # Idempotent, never increments counts.
    with pytest.raises(ValueError, match="cannot renew"):
        u.save_contexts(db, [original], now=NOW)
    later = NOW + timedelta(minutes=1)
    newer = context(later, observed_until=later + timedelta(minutes=1), expires_at=later + timedelta(minutes=1) + u.CONTEXT_TTL)
    u.save_contexts(db, [newer], now=later + timedelta(minutes=1))
    stale = {**original, "closed_at": later + timedelta(minutes=1), "observed_until": later + timedelta(minutes=1),
             "expires_at": later + timedelta(minutes=1) + u.CONTEXT_TTL}
    with pytest.raises(ValueError):
        u.save_contexts(db, [stale], now=later + timedelta(minutes=1))
    assert retained(db) == len(rows(db)) == 2


@pytest.mark.parametrize("skew", [-60, -.5, .5, 30, 60])
def test_db_time_before_identity_not_process_clock(db, skew):
    exporter = u.MetadataExporter(db)
    record = u.normalize_pod(pod())
    exporter.observe([record], at=NOW + timedelta(seconds=skew), now=NOW)
    later = NOW + timedelta(minutes=1)
    exporter.observe([record], at=later + timedelta(seconds=skew), now=later)
    saved = rows(db)[0]
    assert len(rows(db)) == 1 and retained(db) == 1
    assert u._datetime(saved["observed_from"]) == NOW
    assert u._datetime(saved["observed_until"]) == later
    assert saved["context_key"] == u.context_key(record, NOW)


@pytest.mark.parametrize("failure", ["skew", "clock", "sink"])
def test_failures_drop_continuity(db, monkeypatch, failure):
    exporter = u.MetadataExporter(db)
    record = u.normalize_pod(pod())
    observe(exporter, [record])
    def fail(*args, **kwargs):
        raise RuntimeError("offline")
    if failure == "clock":
        monkeypatch.setattr(u, "database_now", fail)
    if failure == "sink":
        monkeypatch.setattr(u, "save_contexts", fail)
    with pytest.raises((ValueError, RuntimeError)):
        exporter.observe([record], at=NOW + timedelta(seconds=61) if failure == "skew" else NOW, now=NOW)
    assert not exporter.live and exporter.last_heartbeat is None
    assert u._datetime(rows(db)[0]["observed_until"]) == NOW


def test_commit_clock_freshness_fence_rolls_back(db, monkeypatch):
    before = NOW + timedelta(hours=4)
    monkeypatch.setattr(u, "utc_now", lambda: before)
    clocks = iter([before.isoformat(), (before + timedelta(seconds=1)).isoformat()])
    with db.connect() as c:
        c.connection.driver_connection.create_function("current_timestamp", 0, lambda: next(clocks))
    with pytest.raises(ValueError, match="stale"):
        u.save_contexts(db, [context(before)])
    assert not rows(db) and retained(db) == 0


def test_clock_override_offline_sqlite_only():
    class Connection:
        dialect = type("Dialect", (), {"name": "mysql"})()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, *args):
            pytest.fail("override must fail before SQL")
    class Engine:
        def connect(self):
            return Connection()
    with pytest.raises(ValueError, match="offline SQLite"):
        u.database_now(Engine(), now=NOW)
    with pytest.raises(ValueError, match="offline SQLite"):
        u.save_contexts(Engine(), [context()], now=NOW)


def test_partial_flush_failure_discards_all_continuity(db, monkeypatch):
    real_save = u.save_contexts
    calls = []
    def save(engine, contexts, **kwargs):
        calls.append(len(contexts))
        if len(calls) == 2:
            raise RuntimeError("second transaction offline")
        return real_save(engine, contexts, **kwargs)
    monkeypatch.setattr(u, "save_contexts", save)
    records = [{**u.normalize_pod(pod()), "pod_uid": f"pod-{i}"} for i in range(1001)]
    exporter = u.MetadataExporter(db)
    with pytest.raises(RuntimeError, match="second transaction"):
        observe(exporter, records, heartbeat=True)
    assert calls == [1000, 1] and not exporter.live
    assert len(rows(db)) == retained(db) == 1000


def test_node_pagination_reuses_token_provider_and_ca(monkeypatch):
    tokens = iter(["first", "rotated"])
    calls = []
    def get(url, *, headers, ca_file):
        calls.append((url, headers, ca_file))
        return {"items": [node()], "metadata": {"continue": "next" if len(calls) == 1 else "", "resourceVersion": "rv"}}
    monkeypatch.setattr(watcher, "_get_json", get)
    client = watcher._KubernetesClient(base_url="https://kube", token_provider=lambda: next(tokens), ca_file="existing-ca", watch_timeout_seconds=300)
    items, rv = client.list_nodes()
    assert len(items) == 2 and rv == "rv"
    assert calls[0][0] == "https://kube/api/v1/nodes?limit=500"
    assert "continue=next" in calls[1][0] and "/namespaces/" not in calls[1][0]
    assert calls[0][1] == {"Authorization": "Bearer first"}
    assert calls[1][1] == {"Authorization": "Bearer rotated"} and calls[1][2] == "existing-ca"


def test_node_worker_is_cache_only_and_clears_on_error():
    cache = u.NodeSelectorCache()
    class Stop:
        stopped = False
        waits = []
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            self.waits.append(seconds)
            self.stopped = True
    class Client:
        def list_nodes(self):
            return [node()], "rv"
    stop = Stop()
    watcher._run_utilization_node_worker(node_cache=cache, client=Client(), stop_event=stop)
    assert stop.waits == [60] and cache.snapshot()[1]["node-a"].instance_id == "np-123"
    class FailingClient:
        def list_nodes(self):
            raise RuntimeError("offline")
    watcher._run_utilization_node_worker(node_cache=cache, client=FailingClient(), stop_event=Stop())
    assert cache.snapshot()[1] == {}


def _run_enabled_watch(monkeypatch, events, *, initial=None, client=None, stop=None, health_state=None, node_cache=None):
    """Real normalization/exporter, injected lifecycle and offline watch transport."""
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
        if kwargs.get("on_lifecycle_rows") is not None:
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
        health_state=health_state or watcher.WatchHealthState(stale_after_seconds=720), recorder=Recorder(),
        jenkins_prefix_cache=None, stop_event=stop, node_cache=node_cache,
    )
    return lifecycle


@pytest.mark.parametrize("configured", ["", "repo, job", "owner"])
def test_invalid_label_configuration_disables_only_utilization(monkeypatch, configured, caplog):
    monkeypatch.setenv("CI_DASHBOARD_UTILIZATION_LABEL_KEYS", configured)
    stop = threading.Event()

    class Client:
        lists = 0

        def list_collection(self, **kwargs):
            self.lists += 1
            return [pod()], "rv"

        def stream_collection(self, **kwargs):
            assert "utilization_batches" not in kwargs
            yield {"type": "MODIFIED", "object": pod()}
            stop.set()

    client = Client()
    lifecycle = _run_enabled_watch(monkeypatch, [], client=client, stop=stop)
    assert len(lifecycle) == 2 and client.lists == 1
    assert "invalid utilization label configuration" in caplog.text


def _offline_clock(monkeypatch, db):
    clock = [NOW]
    real_save = u.save_contexts
    monkeypatch.setattr(u, "database_now", lambda *args, **kwargs: clock[0])
    monkeypatch.setattr(u, "utc_now", lambda: clock[0])
    monkeypatch.setattr(u, "save_contexts", lambda engine, contexts: real_save(db, contexts, now=clock[0]))
    return clock


@pytest.mark.parametrize("kind", ["DELETED", "Succeeded", "Failed"])
def test_clean_eof_last_rv_continuation_and_no_terminal_phantom(db, monkeypatch, kind):
    clock = _offline_clock(monkeypatch, db)
    stop = threading.Event()
    health = watcher.WatchHealthState(stale_after_seconds=720)
    monkeypatch.setattr(watcher.time, "monotonic", lambda: (clock[0] - NOW).total_seconds())
    ending = pod()
    ending["metadata"]["uid"] = "ending"
    terminal = copy.deepcopy(ending)
    terminal["status"] = {"phase": "Succeeded" if kind == "DELETED" else kind}
    postmortem = copy.deepcopy(terminal)
    postmortem["metadata"]["uid"] = "postmortem"
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
                ended = copy.deepcopy(terminal)
                ended["metadata"]["resourceVersion"] = "end-rv"
                yield {"type": "DELETED" if kind == "DELETED" else "MODIFIED", "object": ended}
            elif cycle == 2:
                yield {"type": "MODIFIED", "object": postmortem}
                yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "bookmark-rv"}}}
            elif cycle == 4:
                stop.set()
    client = Client()
    _run_enabled_watch(monkeypatch, [], client=client, stop=stop, health_state=health)
    assert client.lists == 1
    assert client.watches == ["list-rv", "end-rv", "bookmark-rv", "bookmark-rv"]
    saved = {row["pod_uid"]: row for row in rows(db)}
    assert len(saved) == retained(db) == 2
    assert u._datetime(saved["pod-uid"]["observed_until"]) == NOW + timedelta(minutes=15)
    assert u._datetime(saved["ending"]["closed_at"]) == NOW  # Terminal event is not live evidence.


def test_reused_node_ip_requires_new_evidence_and_bookmarks_do_not_renew_old_binding(db, monkeypatch):
    clock = _offline_clock(monkeypatch, db)
    monkeypatch.setattr(watcher.time, "monotonic", lambda: (clock[0] - NOW).total_seconds())
    address = "10.0.0.8"
    cache = u.NodeSelectorCache()
    old_node = node(address, "ins-old")
    next_node = node(address, "ins-next")
    next_node["metadata"].update(uid="next", creationTimestamp="2026-11-24T04:01:30Z")
    old_pod = pod()
    old_pod["spec"]["nodeName"] = address
    new_pod = copy.deepcopy(old_pod)
    new_pod["metadata"].update(uid="new", creationTimestamp="2026-11-24T04:01:45Z")
    bookmark = {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "rv"}}}
    cache.refresh([old_node], listed_at=-1)

    def events():
        clock[0] = NOW + timedelta(minutes=1)
        cache.refresh([old_node], listed_at=60)
        yield bookmark
        clock[0] = NOW + timedelta(minutes=2)
        yield {"type": "ADDED", "object": new_pod}
        yield bookmark
        clock[0] = NOW + timedelta(minutes=3)
        cache.refresh([next_node], listed_at=180)
        yield bookmark
        for minute in range(4, 46):
            clock[0] = NOW + timedelta(minutes=minute)
            cache.refresh([next_node], listed_at=minute * 60)
            yield bookmark

    lifecycle = _run_enabled_watch(monkeypatch, events(), initial=[old_pod], node_cache=cache)
    assert len(lifecycle) == 2  # No object update after refresh for either Pod.
    assert [(row["pod_uid"], row["node_query_value"], u._datetime(row["observed_from"]),
             u._datetime(row["observed_until"])) for row in rows(db)] == [
        ("pod-uid", "", NOW, NOW + timedelta(minutes=1)),
        ("pod-uid", "ins-old", NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)),
        ("new", "", NOW + timedelta(minutes=2), NOW + timedelta(minutes=3)),
        ("pod-uid", "", NOW + timedelta(minutes=2), NOW + timedelta(minutes=45)),
        ("new", "ins-next", NOW + timedelta(minutes=3), NOW + timedelta(minutes=45)),
    ]


@pytest.mark.parametrize("repeated", [False, True])
def test_read_burst_coalescing_bound_and_stop_flush(monkeypatch, repeated):
    batches = []
    monkeypatch.setattr(watcher, "_export_utilization", lambda exporter, records, **kwargs: batches.append(copy.deepcopy(records)))
    def events():
        for number in range(1001):
            obj = pod()
            obj["metadata"]["uid"] = "same" if repeated else f"pod-{number}"
            obj["spec"]["nodeName"] = f"10.0.{number // 256}.{number % 256}"
            yield {"type": "MODIFIED", "object": obj}
            if number == 999:
                assert len(batches) == 1
        yield {"type": watcher.UTILIZATION_BATCH_END}
        assert len(batches) == 2
    assert len(_run_enabled_watch(monkeypatch, events())) == 1001
    assert [len(batch) for batch in batches] == ([1, 1] if repeated else [1000, 1])
    assert batches[-1][0]["_node_ref"].name == "10.0.3.232"
    assert batches[-1][0]["node_query_value"] == ""


def test_event_processing_deadline_flushes(monkeypatch):
    batches, clock = [], [0.0]
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
def test_terminal_flushes_pending_before_closure(monkeypatch, kind):
    batches = []
    monkeypatch.setattr(watcher, "_export_utilization", lambda exporter, records, **kwargs: batches.append((records, kwargs)))
    ended = pod()
    if kind != "DELETED":
        ended["status"] = {"phase": kind}
    _run_enabled_watch(monkeypatch, [{"type": "ADDED", "object": pod()},
                                   {"type": "DELETED" if kind == "DELETED" else "MODIFIED", "object": ended}])
    assert len(batches) == 2 and not batches[0][1].get("deleted") and batches[1][1]["deleted"]


@pytest.mark.parametrize("failure", ["gap", "watch_410", "http_410", "exception", "no_cursor", "401"])
def test_reconnect_relist_closes_old_at_last_evidence(db, monkeypatch, failure):
    clock = _offline_clock(monkeypatch, db)
    monkeypatch.setattr(watcher, "_sleep_until_retry", lambda stop: None)
    stop = threading.Event()
    class Client:
        lists = 0
        def list_collection(self, **kwargs):
            self.lists += 1
            if self.lists == 2:
                clock[0] = NOW + timedelta(minutes=2)
            return [pod()], None if failure == "no_cursor" else f"list-{self.lists}"
        def stream_collection(self, **kwargs):
            if self.lists == 2:
                stop.set()
                return
            clock[0] = NOW + timedelta(seconds=1)
            yield {"type": "MODIFIED", "object": pod()}
            if failure == "gap":
                yield {"type": watcher.UTILIZATION_GAP}
            elif failure in {"watch_410", "401"}:
                yield {"type": "ERROR", "object": {"code": 410 if failure == "watch_410" else 401, "reason": "Expired"}}
            elif failure == "http_410":
                raise watcher.urllib_error.HTTPError("https://offline.test", 410, "expired", {}, None)
            elif failure == "exception":
                raise RuntimeError("offline disconnect")
    client = Client()
    _run_enabled_watch(monkeypatch, [], client=client, stop=stop)
    assert client.lists == 2 and len(rows(db)) == retained(db) == 2
    assert u._datetime(rows(db)[0]["closed_at"]) == NOW + timedelta(seconds=1)
    assert u._datetime(rows(db)[1]["observed_from"]) == NOW + timedelta(minutes=2)


@pytest.mark.parametrize("stage", ["event_flush", "bookmark", "eof_heartbeat"])
def test_export_failure_forces_relist_and_never_replays(db, monkeypatch, stage):
    clock = _offline_clock(monkeypatch, db)
    real_save = u.save_contexts
    calls = []
    def save(engine, contexts):
        calls.append(copy.deepcopy(contexts))
        if len(calls) == 2:
            raise RuntimeError("offline metadata sink")
        return real_save(engine, contexts)
    monkeypatch.setattr(u, "save_contexts", save)
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
                stop.set()
                return
            clock[0] = NOW + timedelta(minutes=5)
            if stage == "event_flush":
                yield {"type": "MODIFIED", "object": pod()}
            elif stage == "bookmark":
                yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "next"}}}
    client = Client()
    _run_enabled_watch(monkeypatch, [], client=client, stop=stop)
    assert client.lists == 2 and len(rows(db)) == retained(db) == 2
    assert u._datetime(rows(db)[0]["observed_until"]) == NOW
    assert u._datetime(rows(db)[0]["closed_at"]) == NOW
    assert u._datetime(rows(db)[1]["observed_from"]) == NOW + timedelta(minutes=10)
    assert not any(u._datetime(row["observed_from"]) == NOW + timedelta(minutes=5) for row in rows(db))
