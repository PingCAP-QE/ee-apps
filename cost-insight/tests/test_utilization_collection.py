"""Offline collection/aggregation tests; SDK calls are always mocked."""

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, select, text

from cost_insight.utilization import policy, runner, store, tencent
from cost_insight.utilization.aggregate import (
    aggregate_day,
    day_bounds,
    identity,
    resource_class,
    utc,
)
from cost_insight.utilization.schema import daily, metadata, observations

DAY = date(2026, 11, 23)
START = utc(policy.day_start(DAY))
NOW = START + timedelta(hours=12)
EXAMPLE = Path(__file__).parents[1] / "config/tencent-utilization.example.json"


@pytest.fixture
def profile():
    result = tencent.load_profile(str(EXAMPLE), probe=True)
    for entries in result["contracts"].values():
        for entry in entries.values():
            entry.update(
                verified_horizon_seconds=3 * 86400,
                horizon_verified_at=START.isoformat(),
                evidence_reference="offline fixture, NOT a real canary",
                timestamp_alignment_verified=True,
            )
            for contract in (entry["contract"], *entry.get("scope_contracts", {}).values()):
                if contract["unit"] == "MBytes":
                    contract["source_scale"] = 1_000_000
    return result


def observation(kind="node", entity="np-one", *, cpu=2, begin=START, end=None, **attrs):
    end = end or begin + timedelta(minutes=1)
    return {
        "resource_type": kind,
        "resource_id": entity,
        "provider_id": entity if kind != "pod" else "",
        "cluster": "cls-9zpon9y1",
        "namespace": "jenkins-tidb" if kind == "pod" else "",
        "name": entity,
        "observed_from": begin.replace(tzinfo=None),
        "last_seen": end.replace(tzinfo=None),
        "observed_until": end.replace(tzinfo=None),
        "attributes_json": {
            "cpu_capacity": cpu,
            "memory_capacity": 2_000_000,
            "allocatable_cpu": cpu,
            "allocatable_memory": 2_000_000,
            "region": "ap-beijing",
            **attrs,
        },
    }


def point(
    kind="node",
    entity="np-one",
    value=50,
    *,
    metric="cpu",
    stamp=START,
    node="np-one",
    **dimensions,
):
    return {
        "kind": kind,
        "entity": entity,
        "metric": metric,
        "timestamp": stamp,
        "value": value,
        "node_id": node,
        "region": "ap-beijing",
        **dimensions,
    }


def metric(rows, scope="account_class", name="cpu", cls="tke_native"):
    return next(r for r in rows if r["scope"] == scope and r["resource_class"] == cls)[
        "metrics_json"
    ][name]


def aggregate(profile, points, obs, minutes=1, **kwargs):
    return aggregate_day(
        DAY, points, obs, profile["contracts"], cutoff=START + timedelta(minutes=minutes), **kwargs
    )


def test_weighted_mean_and_matching_memory_units(profile):
    obs = [observation(entity="np-two", cpu=2), observation(entity="np-32", cpu=32)]
    rows = aggregate(
        profile, [point(entity="np-two", value=90), point(entity="np-32", value=10)], obs
    )
    body = metric(rows)
    assert body["avg"] == pytest.approx(14.70588235)
    assert body["coverage"] == 1
    assert body["used_integral"] == pytest.approx((1.8 + 3.2) * 60)
    assert metric(rows, name="memory")["avg"] is None
    assert metric(rows, name="memory")["coverage"] == 0


def test_peak_is_time_aligned_not_sum_of_unrelated_maxima(profile):
    obs = [
        observation(entity="np-a", end=START + timedelta(minutes=2)),
        observation(entity="np-b", end=START + timedelta(minutes=2)),
    ]
    points = [
        point(entity="np-a", value=90),
        point(entity="np-b", value=10),
        point(entity="np-a", value=10, stamp=START + timedelta(minutes=1)),
        point(entity="np-b", value=90, stamp=START + timedelta(minutes=1)),
    ]
    body = metric(aggregate(profile, points, obs, minutes=2))
    assert body["avg"] == body["peak"] == body["latest"] == 50
    assert body["peak_coverage"] == 1


def test_missing_point_excluded_from_both_mean_integrals_not_zero(profile):
    obs = [observation(end=START + timedelta(minutes=2))]
    body = metric(aggregate(profile, [point(value=80)], obs, minutes=2))
    assert body["avg"] == 80
    assert body["coverage"] == 0.5
    assert "missing_points" in body["quality"]
    assert body["observed_seconds"] == 60 and body["expected_seconds"] == 120


def test_incomplete_simultaneous_bucket_is_not_account_peak(profile):
    obs = [observation(entity="np-a"), observation(entity="np-b")]
    body = metric(aggregate(profile, [point(entity="np-a", value=90)], obs))
    assert body["avg"] == 90 and body["peak"] is None
    assert body["coverage"] == 0.5


def test_pod_scope_uses_requests_without_author_and_preserves_memory_scale(profile):
    pod = observation(
        "pod",
        "build-abc",
        cpu_request=2,
        memory_request=2_000_000,
        repo="pingcap/tidb",
        job_name="tidb/unit",
        node_name="np-one",
    )
    entity = "cls-9zpon9y1|jenkins-tidb|build-abc"
    rows = aggregate(
        profile, [point("pod", entity, 1), point("pod", entity, 1, metric="memory")], [pod]
    )
    assert {r["scope"] for r in rows} == {"job", "repo"}
    assert metric(rows, "job")["avg"] == 50
    assert metric(rows, "repo", "memory")["avg"] == 50
    assert "author" not in str(rows)
    assert metric(rows, "job")["contract"]["denominator"] == "requests"


def test_unknown_identity_and_zero_requests_do_not_discard_actual_usage(profile):
    entity = "cls-9zpon9y1|jenkins-tidb|build-abc"
    rows = aggregate(profile, [point("pod", entity, 2)], [])
    body = metric(rows, "job")
    assert body["avg"] is None and body["total_used_integral"] == 120
    assert rows[0]["repo"] == "unknown_repo" and rows[0]["job_name"] == "unknown_job"
    pod = observation("pod", "build-abc", cpu_request=0, memory_request=None)
    rows = aggregate(profile, [point("pod", entity, 2)], [pod])
    assert metric(rows, "job")["avg"] is None
    assert metric(rows, "job")["total_used_integral"] == 120


def test_metadata_only_unknown_node_preserves_unknown_consumer_scope(profile):
    rows = aggregate(
        profile, [], [observation("pod", "build-abc", cpu_request=2, memory_request=2_000_000)]
    )
    assert {r["resource_class"] for r in rows} == {"tke_unknown"}
    assert {r["scope"] for r in rows} == {"job", "repo"}
    assert metric(rows, "job", cls="tke_unknown")["avg"] is None


def test_node_name_join_and_no_backdating_or_open_interval_extrapolation(profile):
    node = observation()
    pod = observation(
        "pod", "build-abc", cpu_request=2, memory_request=2_000_000, node_name="np-one"
    )
    rows = aggregate(profile, [], [node, pod])
    assert metric(rows, "job")["avg"] is None
    late = observation(begin=START + timedelta(seconds=30), end=START + timedelta(minutes=2))
    late["observed_until"] = None
    rows = aggregate(profile, [point()], [late], minutes=2)
    assert metric(rows)["coverage"] == 0
    assert metric(rows)["unknown_seconds"] == 60
    assert metric(rows)["avg"] is None  # first minute is not covered by new metadata


def test_ambiguous_evidence_does_not_guess_denominator(profile):
    obs = observation()
    rows = aggregate(profile, [point()], [obs, deepcopy(obs)])
    assert metric(rows)["avg"] is None
    assert "ambiguous_identity" in metric(rows)["quality"]


def test_cvm_backed_tke_node_is_one_machine(profile):
    measurement = {
        "verified_horizon_seconds": 3 * 86400,
        "pod_memory_source_scale": 1_000_000,
        "node_memory_request_source_scale": 1_000_000,
    }
    profile["contracts"] = tencent.build_contracts(measurement, include_tke_cvm=True)
    obs = [observation("node", "ins-one"), observation("cvm", "ins-one")]
    rows = aggregate(profile, [point("node", "ins-one", 10), point("cvm", "ins-one", 90)], obs)
    assert {r["resource_class"] for r in rows} == {"tke_cvm"}
    assert len([r for r in rows if r["scope"] == "resource"]) == 1
    assert metric(rows, cls="tke_cvm")["avg"] == 10


def test_allocation_uses_allocatable_and_source_unit_conversion(profile):
    obs = [observation(allocatable_cpu=1, allocatable_memory=1_000_000)]
    rows = aggregate(
        profile,
        [point(value=0.5, metric="cpu_allocation"), point(value=0.5, metric="memory_allocation")],
        obs,
    )
    assert metric(rows, name="cpu_allocation")["avg"] == 50
    assert metric(rows, name="memory_allocation")["avg"] == 50
    assert metric(rows, name="cpu_allocation")["contract"]["denominator"] == "allocatable"


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1, True])
def test_invalid_points_are_missing_not_zero(profile, value):
    rows = aggregate(profile, [point(value=value)], [observation()])
    assert metric(rows)["avg"] is None


def test_duplicate_points_and_contracts_and_budgets_fail_closed(profile):
    assert aggregate(profile, [point(), point()], [observation()])
    with pytest.raises(ValueError, match="Conflicting"):
        aggregate(profile, [point(), point(value=10)], [observation()])
    with pytest.raises(ValueError, match="budget"):
        aggregate(profile, [point()], [observation()], max_buckets=1)
    with pytest.raises(ValueError, match="align"):
        aggregate(profile, [point(stamp=START + timedelta(seconds=1))], [observation()])
    with pytest.raises(ValueError, match="Date"):
        day_bounds(DAY, START)
    with pytest.raises(TypeError):
        utc(123)
    with pytest.raises(ValueError):
        resource_class("not-a-provider")
    assert identity("a", "b") != identity("ab")
    profile["contracts"]["tke_native"]["cpu"]["contract"]["unit"] = "cores"
    profile["contracts"]["tke_native"]["cpu"]["contract"]["source_scale"] = 0
    with pytest.raises(ValueError, match="source_scale"):
        aggregate(profile, [point()], [observation()])


def test_streamed_windows_merge_same_integrals_and_synchronous_peaks(profile):
    obs = [observation(end=START + timedelta(minutes=2))]
    points = [point(value=10), point(value=80, stamp=START + timedelta(minutes=1))]
    expected = aggregate(profile, points, obs, minutes=2)
    target = {}
    runner.merge_window_rows(target, aggregate(profile, points, obs))
    runner.merge_window_rows(
        target,
        aggregate(profile, points, obs, minutes=2, window_start=START + timedelta(minutes=1)),
    )
    assert metric(list(target.values())) == metric(expected)
    with pytest.raises(ValueError, match="ordered"):
        runner.merge_window_rows(target, expected)


def sdk_response(kind="node", *, metric_name=None, value=50, entity="np-one"):
    name = metric_name or tencent.ALLOWED_METRICS[kind]["cpu"]
    dims = {
        "tke_cluster_instance_id": "cls-9zpon9y1",
        "un_instance_id": entity,
        "namespace": "jenkins-tidb",
        "pod_name": "build-abc",
    }
    return {
        "Period": 60,
        "Data": [
            {
                "MetricName": name,
                "Points": [
                    {
                        "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                        "Values": [{"Timestamp": int(START.timestamp()), "Value": value}],
                    }
                ],
            }
        ],
    }


def api_call(service, action, region, params):
    if action == "GetCallerIdentity":
        return {"Response": {"AccountId": policy.ACCOUNT_ID}}
    if action == "DescribeInstances":
        return {
            "InstanceSet": [
                {
                    "InstanceId": "ins-cvm",
                    "CPU": 2,
                    "Memory": 4,
                    "InstanceType": "test",
                    "CreatedTime": START.isoformat(),
                }
            ],
            "TotalCount": 1,
        }
    if action == "DescribeBaseMetrics":
        kinds = ("cvm",) if params["Namespace"] == "QCE/CVM" else ("node", "pod")
        return {
            "MetricSet": [
                {
                    "MetricName": name,
                    "Unit": "%"
                    if kind == "cvm" or (kind == "node" and not key.endswith("allocation"))
                    else "MBytes"
                    if key.startswith("memory")
                    else "None",
                    "Periods": [{"Period": "60", "StatType": ["avg"]}],
                }
                for kind in kinds
                for key, name in tencent.ALLOWED_METRICS[kind].items()
            ]
        }
    if action == "DescribeStatisticData":
        kind = "pod" if params["MetricNames"][0].startswith("K8sPod") else "node"
        return sdk_response(kind)
    if action == "GetMonitorData":
        return {
            "Period": 60,
            "DataPoints": [
                {
                    "Dimensions": [{"Name": "InstanceId", "Value": "ins-cvm"}],
                    "Timestamps": [int(START.timestamp())],
                    "Values": [50],
                }
            ],
        }
    raise AssertionError("Unexpected API call")


def test_reader_batches_correct_selectors_units_and_auth(profile):
    call = Mock(side_effect=api_call)
    reader = tencent.TencentReader(profile, call=call)
    reader.verify_account()
    reader.verify_contracts()
    reader.verify_contracts()  # cached, not repeated for every window
    inventory = reader.inventory(START)
    points = reader.window(START, START + timedelta(minutes=1), inventory)
    assert {p["kind"] for p in points} == {"node", "pod", "cvm"}
    assert inventory[0]["attributes_json"]["memory_capacity"] == 4 * 1024**3
    requests = [c.args[3] for c in call.call_args_list if c.args[1] == "DescribeStatisticData"]
    assert len(requests[0]["Conditions"]) == 1  # verified Node cluster-only, NO GroupBys
    assert requests[1]["Conditions"][1]["Key"] == "un_instance_id"
    assert reader.stats["calls"] == len(call.call_args_list)
    assert reader.stats["response_bytes"] > 0


def test_profile_example_cannot_enable_unverified_conversion():
    with pytest.raises(ValueError, match="conversion|source_scale"):
        tencent.load_profile(str(EXAMPLE))
    result = tencent.probe(
        tencent.load_profile(str(EXAMPLE), probe=True),
        reader=tencent.TencentReader(
            tencent.load_profile(str(EXAMPLE), probe=True), call=api_call, probe=True
        ),
        now=START + timedelta(minutes=30),
    )
    assert result["dry_run"] and not result["full_replay_horizon_verified"]
    assert result["unit_conversion_verified"] is False
    assert result["metric_metadata"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "another"),
        ("regions", []),
        ("regions", ["bad"]),
        ("cluster_id", "bad"),
        ("namespaces", []),
        ("namespaces", [""]),
    ],
)
def test_profile_rejects_unbounded_or_wrong_scope(profile, field, value):
    profile[field] = value
    with pytest.raises(ValueError):
        tencent.validate_profile(profile)


def test_source_contract_change_wrong_account_and_errors_fail(profile):
    reader = tencent.TencentReader(profile, call=lambda *args: {"AccountId": "another"})
    with pytest.raises(ValueError, match="Credential account"):
        reader.verify_account()
    reader = tencent.TencentReader(profile, call=lambda *args: {"Error": {"Code": "NoPermission"}})
    with pytest.raises(ValueError, match="Cloud returned"):
        reader.call("monitor", "DescribeBaseMetrics", "ap-beijing")
    with pytest.raises(ValueError, match="Non-read-only"):
        reader.call("cvm", "TerminateInstances", "ap-beijing")
    reader.stats["calls"] = 2000
    with pytest.raises(ValueError, match="budget"):
        reader.verify_account()
    reader = tencent.TencentReader(profile, call=lambda *args: {"MetricSet": []})
    with pytest.raises(ValueError, match="contract differs"):
        reader.verify_contracts()


def test_transient_retry_is_bounded_and_no_retry_for_bad_request(profile):
    error = RuntimeError("mock")
    error.code = "RequestLimitExceeded"
    call = Mock(side_effect=[error, error, {"AccountId": policy.ACCOUNT_ID}])
    sleep = Mock()
    reader = tencent.TencentReader(profile, call=call, sleep=sleep)
    reader.verify_account()
    assert reader.stats["calls"] == 3 and sleep.call_count == 2
    call.side_effect = error
    with pytest.raises(RuntimeError):
        reader.verify_account()


def test_sdk_adapter_uses_official_client_without_network(profile, monkeypatch):
    from tencentcloud.common.abstract_client import AbstractClient

    reader = tencent.TencentReader(profile)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="credentials"):
        reader.verify_account()
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "offline-test")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "offline-test-not-a-secret")
    monkeypatch.setattr(
        AbstractClient,
        "call_json",
        lambda *args, **kwargs: {"Response": {"AccountId": policy.ACCOUNT_ID}},
    )
    reader.verify_account()
    reader.verify_account()
    assert len(reader._clients) == 1


@pytest.fixture
def engine(tmp_path, profile):
    engine = create_engine(f"sqlite:///{tmp_path / 'collection.db'}")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE cost_job_state (job_name TEXT PRIMARY KEY,watermark_json JSON NOT NULL,last_started_at DATETIME,last_succeeded_at DATETIME,last_status TEXT NOT NULL,last_error TEXT,updated_at DATETIME)"
            )
        )
    store.configure(engine, start_week=DAY, contracts=profile["contracts"], enabled=True, now=NOW)
    store.maintain(engine, now=NOW)
    cvm = observation("cvm", "ins-cvm", begin=START, end=NOW)
    store.save_observations(engine, [cvm], now=NOW)
    yield engine
    engine.dispose()


class FixtureReader:
    def __init__(self, *, fail_after=None):
        self.stats = {"windows": 0}
        self.fail_after = fail_after

    def verify_account(self):
        pass

    def verify_contracts(self):
        pass

    def window(self, start, end, observations):
        self.stats["windows"] += 1
        if self.fail_after and self.stats["windows"] >= self.fail_after:
            raise RuntimeError("offline source outage")
        return [
            point("cvm", "ins-cvm", 50, stamp=start),
            point("cvm", "ins-cvm", 50, metric="memory", stamp=start),
        ]


def test_runner_end_to_end_replaces_instead_of_adding_and_dry_run_select_only(engine, profile):
    first = runner.sync_daily(engine, profile, now=NOW, reader=FixtureReader())
    assert first["days"][0]["skipped"] and first["days"][1]["published"]
    with engine.connect() as connection:
        before = connection.execute(select(daily)).mappings().all()
    second = runner.sync_daily(engine, profile, now=NOW, reader=FixtureReader())
    assert second["days"][1]["published"]
    with engine.connect() as connection:
        after = connection.execute(select(daily)).mappings().all()
    assert [r["metrics_json"] for r in before] == [r["metrics_json"] for r in after]
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda conn, cursor, sql, parameters, context, executemany: statements.append(sql),
    )
    runner.sync_daily(engine, profile, now=NOW, dry_run=True, reader=FixtureReader())
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)


def test_runner_partial_failure_preserves_trusted_day_and_marks_stale(engine, profile):
    runner.sync_daily(engine, profile, now=NOW, reader=FixtureReader())
    with engine.connect() as connection:
        before = connection.execute(select(daily.c.metrics_json)).scalars().all()
    result = runner.sync_daily(engine, profile, now=NOW, reader=FixtureReader(fail_after=3))
    assert result["days"][1]["published"] is False
    with engine.connect() as connection:
        assert set(connection.execute(select(daily.c.status)).scalars()) == {"stale"}
        assert connection.execute(select(daily.c.metrics_json)).scalars().all() == before


def test_runner_explicit_date_guard_contract_match_and_inventory(engine, profile):
    with pytest.raises(ValueError, match="require --repair"):
        runner.sync_daily(engine, profile, now=NOW, usage_date=DAY, reader=FixtureReader())
    with pytest.raises(ValueError, match="outside"):
        runner.sync_daily(
            engine,
            profile,
            now=NOW,
            usage_date=DAY - timedelta(days=7),
            repair=True,
            reader=FixtureReader(),
        )
    changed = deepcopy(profile)
    changed["contracts"]["cvm"]["cpu"]["contract"]["semantic_version"] = "v2"
    with pytest.raises(ValueError, match="exact"):
        runner.sync_daily(engine, changed, now=NOW, reader=FixtureReader())
    result = runner.sync_inventory(
        engine, profile, dry_run=True, now=NOW, reader=tencent.TencentReader(profile, call=api_call)
    )
    assert result["inventory"]["dry_run"]
    with engine.connect() as connection:
        assert connection.execute(select(observations.c.id)).all()


def test_kubernetes_provider_uri_matches_cloud_instance_and_pod_node_name(profile):
    node = observation()
    node.update(provider_id="qcloud:///ap-beijing/np-one", name="10.0.0.1")
    pod = observation(
        "pod", "build-abc", cpu_request=2, memory_request=2_000_000, node_name="10.0.0.1"
    )
    rows = aggregate(profile, [point(value=60)], [node, pod])
    assert metric(rows)["avg"] == 60
    assert metric(rows)["coverage"] == 1
    assert {r["resource_class"] for r in rows} == {"tke_native"}


def test_profile_source_scope_is_pinned_not_just_metric_names(tmp_path):
    import json

    first = tencent.load_profile(str(EXAMPLE), probe=True)
    changed = json.loads(EXAMPLE.read_text())
    changed["namespaces"].append("another-ci-namespace")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(changed))
    second = tencent.load_profile(str(path), probe=True)
    assert first["contracts"] != second["contracts"]


def test_clock_horizon_and_disabled_runner_fail_closed(engine, profile, monkeypatch):
    assert runner.database_now(engine).tzinfo is not None
    fake = Mock()
    fake.dialect.name = "mysql"
    with pytest.raises(ValueError, match="Clock injection"):
        runner.database_now(fake, NOW)
    blocked = deepcopy(profile)
    for entries in blocked["contracts"].values():
        for entry in entries.values():
            entry["verified_horizon_seconds"] = 1
    store_state = store.load_state(engine)
    monkeypatch.setattr(
        store, "load_state", lambda _engine: {**store_state, "contracts": blocked["contracts"]}
    )
    result = runner.sync_daily(engine, blocked, now=NOW, dry_run=True, reader=FixtureReader())
    assert result["days"][-1]["reason"] == "unverified_or_exhausted_source_horizon"


def test_commands_are_explicit_opt_in_and_route_without_network(
    engine, profile, monkeypatch, capsys
):
    from types import SimpleNamespace

    from cost_insight.jobs import cli

    monkeypatch.setattr(cli, "get_settings", lambda **kwargs: SimpleNamespace(log_level="ERROR"))
    monkeypatch.setattr(cli, "configure_logging", lambda *_: None)
    fake_engine = Mock()
    monkeypatch.setattr(cli, "build_engine", lambda *_: fake_engine)
    monkeypatch.setattr(tencent, "load_profile", lambda *args, **kwargs: profile)
    probe = Mock(return_value={"dry_run": True})
    monkeypatch.setattr(tencent, "probe", probe)
    assert cli.main(["utilization-probe", "--profile", "offline"]) == 0
    fake_engine.dispose.assert_not_called()
    configure = Mock(return_value={"enabled": False})
    monkeypatch.setattr(store, "configure", configure)
    assert (
        cli.main(
            [
                "utilization-configure",
                "--profile",
                "offline",
                "--start-week",
                "2026-11-23",
                "--dry-run",
            ]
        )
        == 0
    )
    assert configure.call_args.kwargs["enabled"] is False
    with pytest.raises(ValueError, match="confirm-source-horizons"):
        cli.main(
            [
                "utilization-configure",
                "--profile",
                "offline",
                "--start-week",
                "2026-11-23",
                "--enable",
            ]
        )
    profile["contracts"]["cvm"]["cpu"]["horizon_verified_at"] = None
    with pytest.raises(ValueError, match="canary evidence"):
        cli.main(
            [
                "utilization-configure",
                "--profile",
                "offline",
                "--start-week",
                "2026-11-23",
                "--enable",
                "--confirm-source-horizons",
            ]
        )
    maintain = Mock(return_value={"dry_run": True})
    monkeypatch.setattr(store, "maintain", maintain)
    assert cli.main(["utilization-maintain", "--dry-run"]) == 0
    inventory = Mock(return_value={"inventory": {"dry_run": True}})
    monkeypatch.setattr(runner, "sync_inventory", inventory)
    assert (
        cli.main(["sync-tencent-utilization-inventory", "--profile", "offline", "--dry-run"]) == 0
    )
    sync = Mock(return_value={"days": [{"published": False}]})
    monkeypatch.setattr(runner, "sync_daily", sync)
    assert (
        cli.main(
            [
                "sync-tencent-utilization",
                "--profile",
                "offline",
                "--date",
                "2026-11-23",
                "--repair",
                "--dry-run",
            ]
        )
        == 2
    )
    assert sync.call_args.kwargs["repair"] is True
    assert "dry_run" in capsys.readouterr().out


def test_suspended_cronjobs_ship_cleanup_separately_without_kubernetes_tokens():
    import json
    import subprocess
    import sys

    path = Path(__file__).parents[1] / "scripts/render-utilization-cronjobs.py"
    result = subprocess.run(
        [
            sys.executable,
            str(path),
            "--namespace",
            "jobs",
            "--image",
            "ghcr.io/example/jobs:reviewed",
            "--db-secret",
            "db",
            "--credential-secret",
            "readonly-tencent",
            "--profile-configmap",
            "utilization",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    items = json.loads(result.stdout)["items"]
    assert len(items) == 3 and all(item["spec"]["suspend"] for item in items)
    assert all(item["spec"]["timeZone"] == "Asia/Shanghai" for item in items)
    pods = [item["spec"]["jobTemplate"]["spec"]["template"]["spec"] for item in items]
    assert all(pod["automountServiceAccountToken"] is False for pod in pods)
    assert pods[0]["containers"][0]["envFrom"] == [{"secretRef": {"name": "db"}}]
    assert "volumes" not in pods[0]
    assert items[-1]["spec"]["schedule"] == "30 0,6,12,18 * * *"


@pytest.mark.parametrize("writer", ["daily", "observation"])
def test_writer_crossing_expiry_rolls_back_before_commit(engine, profile, monkeypatch, writer):
    deadline = policy.expires_at(DAY)
    before = deadline - timedelta(seconds=1)
    store.maintain(engine, now=before)
    count = store.load_state(engine)["retained_rows"]
    monkeypatch.setattr(
        store, "_clock", Mock(side_effect=[before, deadline + timedelta(seconds=1)])
    )
    monkeypatch.setattr(policy, "replay_allowed", lambda *args: True)  # isolate the DB hard fence
    with pytest.raises(ValueError, match="expired, closed"):
        if writer == "daily":
            rows = aggregate(
                profile, [point("cvm", "ins-cvm", 50)], [observation("cvm", "ins-cvm")]
            )
            store.replace_day(engine, DAY, rows, now=before)
        else:
            store.save_observations(engine, [observation("node", "np-new")], now=before)
    assert store.load_state(engine)["retained_rows"] == count
    with engine.connect() as connection:
        assert not connection.execute(select(daily)).all()
        assert len(connection.execute(select(observations)).all()) == 1


def test_zero_requests_is_known_not_unknown_and_keeps_absolute_usage(profile):
    entity = "cls-9zpon9y1|jenkins-tidb|build-abc"
    pod = observation("pod", "build-abc", cpu_request=0, memory_request=0)
    body = metric(aggregate(profile, [point("pod", entity, 2)], [pod]), "job")
    assert body["avg"] is None and body["peak"] is None
    assert body["total_used_integral"] == body["used_integral"] == 120
    assert body["capacity_integral"] == 0 and body["coverage"] == 1
    assert body["unknown_seconds"] == 0 and body["zero_capacity_seconds"] == 60
    assert "zero_requests" in body["quality"] and "unknown_capacity" not in body["quality"]


def test_numeric_coverage_with_one_missing_minute_and_separate_unknown_time(profile):
    obs = observation(end=START + timedelta(minutes=100))
    points = [point(stamp=START + timedelta(minutes=i)) for i in range(99)]
    body = metric(aggregate(profile, points, [obs], minutes=100))
    assert body["coverage"] == pytest.approx(0.99)
    assert body["unknown_seconds"] == 0 and "missing_points" in body["quality"]
    rows = aggregate(profile, [point()], [])
    assert metric(rows)["coverage"] == 0 and metric(rows)["unknown_seconds"] == 60


def test_default_tke_cvm_consumer_contract_does_not_block_or_double_count_machines(profile):
    assert profile["contracts"]["tke_cvm"]["cpu"]["consumer_only"]
    entity = "cls-9zpon9y1|jenkins-tidb|build-abc"
    obs = [
        observation("node", "ins-one"),
        observation("cvm", "ins-one"),
        observation("pod", "build-abc", cpu_request=2, memory_request=2_000_000),
    ]
    points = [
        point("node", "ins-one", 90, node="ins-one"),
        point("cvm", "ins-one", 20),
        point("pod", entity, 1, node="ins-one"),
    ]
    rows = aggregate(profile, points, obs)
    assert metric(rows, cls="cvm")["avg"] == 20
    assert metric(rows, "job", cls="tke_cvm")["avg"] == 50
    assert len([r for r in rows if r["scope"] == "resource"]) == 1
    assert not any(r["scope"] == "account_class" and r["resource_class"] == "tke_cvm" for r in rows)


def test_virtual_node_does_not_abort_other_scopes_and_keeps_pod_usage(profile):
    entity = "cls-9zpon9y1|jenkins-tidb|virtual-pod"
    points = [
        point(),
        point("node", "virtual-kubelet", 30, node="virtual-kubelet"),
        point("pod", entity, 2, node="virtual-kubelet"),
    ]
    rows = aggregate(profile, points, [observation()])
    assert metric(rows)["avg"] == 50
    assert "unmapped_physical_source" in metric(rows)["quality"]
    assert metric(rows)["peak_coverage"] is None
    assert metric(rows, "job", cls="tke_unknown")["total_used_integral"] == 120
    reader = tencent.TencentReader(profile, call=api_call)
    parsed = []
    assert reader._parse_tke(
        sdk_response(entity="virtual-kubelet"), "node", parsed, START, START + timedelta(minutes=1)
    ) == ["virtual-kubelet"]
    assert parsed and reader.stats["unmapped_node_series"] == 1


def test_reader_default_cvm_fallback_queries_historical_tke_ins_ids(profile):
    requests = []

    def call(service, action, region, params):
        if action == "DescribeStatisticData":
            kind = "pod" if params["MetricNames"][0].startswith("K8sPod") else "node"
            return sdk_response(kind, entity="ins-host")
        if action == "GetMonitorData":
            requests.append(params)
            return {
                "Period": 60,
                "DataPoints": [
                    {
                        "Dimensions": [{"Name": "InstanceId", "Value": "ins-host"}],
                        "Timestamps": [int(START.timestamp())],
                        "Values": [20],
                    }
                ],
            }
        return api_call(service, action, region, params)

    reader = tencent.TencentReader(profile, call=call)
    points = reader.window(START, START + timedelta(minutes=1), [])
    assert any(p["kind"] == "cvm" and p["entity"] == "ins-host" for p in points)
    assert requests and all(
        r["Instances"][0]["Dimensions"][0]["Value"] == "ins-host" for r in requests
    )


def empty_proof(profile, cls="tke_native"):
    return {
        "vendor": "tencent",
        "account_id": policy.ACCOUNT_ID,
        "usage_date": DAY.isoformat(),
        "cutoff": (NOW - timedelta(minutes=10)).isoformat(),
        "resource_classes": [cls],
        "source_scope_hash": profile["contracts"][cls]["cpu"]["contract"]["source_scope_hash"],
        "verified_at": NOW.isoformat(),
        "evidence_reference": "offline verified-empty fixture, not production evidence",
        "confirmed_empty": True,
    }


def test_runner_explicit_empty_proof_is_distinct_from_unproved_missing(engine, profile):
    result = runner.sync_daily(
        engine,
        profile,
        usage_date=DAY,
        repair=True,
        empty_proof=empty_proof(profile),
        now=NOW,
        reader=FixtureReader(),
    )
    assert result["days"][0]["published"]
    with engine.connect() as connection:
        row = (
            connection.execute(select(daily).where(daily.c.resource_class == "tke_native"))
            .mappings()
            .one()
        )
        body = row["metrics_json"]["cpu"]
        assert row["scope"] == "account_class" and body["avg"] is None and body["coverage"] == 1
        assert "verified_empty" in body["quality"]
    bad = runner.sync_daily(
        engine,
        profile,
        usage_date=DAY,
        repair=True,
        empty_proof=empty_proof(profile, "cvm"),
        now=NOW,
        reader=FixtureReader(),
    )
    assert not bad["days"][0]["published"]
    with pytest.raises(ValueError, match="explicit"):
        runner.sync_daily(
            engine, profile, empty_proof=empty_proof(profile), now=NOW, reader=FixtureReader()
        )


@pytest.mark.parametrize("changed,lag", [(False, 30), (True, 30), (False, 60)])
def test_cvm_sampled_week_bridge_requires_fresh_identical_confirmation(engine, changed, lag):
    boundary = START + timedelta(days=7)
    before = boundary - timedelta(minutes=15)
    after = before + timedelta(minutes=lag)
    old = observation("cvm", "ins-bridge", begin=before, end=before)
    old["observed_until"] = None
    store.maintain(engine, now=before)
    store.save_observations(engine, [old], now=before)
    new = observation("cvm", "ins-bridge", begin=after, end=after, cpu=32 if changed else 2)
    new["observed_until"] = None
    store.maintain(engine, now=after)
    store.save_observations(engine, [new], now=after)
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(observations)
                .where(observations.c.resource_id == "ins-bridge")
                .order_by(observations.c.observed_from)
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    if not changed and lag <= 45:
        assert (
            rows[0]["observed_until"] == rows[1]["observed_from"] == boundary.replace(tzinfo=None)
        )
    else:
        assert rows[0]["last_seen"] == before.replace(tzinfo=None)
        assert rows[1]["observed_from"] == after.replace(tzinfo=None)
    assert rows[1]["expires_at"] - rows[0]["expires_at"] == timedelta(days=7)


def test_cvm_changed_snapshot_does_not_fabricate_old_capacity_in_poll_gap(engine):
    old = observation("cvm", "ins-change", begin=NOW, end=NOW)
    old["observed_until"] = None
    store.save_observations(engine, [old], now=NOW)
    after = NOW + timedelta(minutes=30)
    new = observation("cvm", "ins-change", begin=after, end=after, cpu=32)
    new["observed_until"] = None
    store.save_observations(engine, [new], now=after)
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(observations)
                .where(observations.c.resource_id == "ins-change")
                .order_by(observations.c.observed_from)
            )
            .mappings()
            .all()
        )
    assert rows[0]["observed_until"] == NOW.replace(tzinfo=None)
    assert rows[1]["observed_from"] == after.replace(tzinfo=None)


def test_partial_metric_reread_and_stale_cutoff_cannot_replace_trusted_values(engine, profile):
    runner.sync_daily(engine, profile, now=NOW, reader=FixtureReader())
    with engine.connect() as connection:
        before = [dict(row) for row in connection.execute(select(daily)).mappings()]
    earlier = deepcopy(before)
    for row in earlier:
        row["cutoff"] -= timedelta(minutes=1)
    rejected = store.replace_day(engine, DAY, earlier, now=NOW)
    assert rejected["reason"] == "older_daily_cutoff"

    class MissingCpu(FixtureReader):
        def window(self, start, end, observations):
            return [point("cvm", "ins-cvm", 50, metric="memory", stamp=start)]

    result = runner.sync_daily(engine, profile, now=NOW, reader=MissingCpu())
    assert not result["days"][-1]["published"]
    with engine.connect() as connection:
        after = connection.execute(select(daily)).mappings().all()
    assert [r["metrics_json"] for r in before] == [r["metrics_json"] for r in after]
    assert {r["status"] for r in after} == {"stale"}


def test_completed_empty_proof_survives_later_successful_nonempty_scope_reread(engine, profile):
    later = NOW + timedelta(days=1)
    store.maintain(engine, now=later)
    proof = empty_proof(profile)
    proof.update(
        cutoff=utc(policy.day_start(DAY + timedelta(days=1))).isoformat(),
        verified_at=later.isoformat(),
    )
    first = runner.sync_daily(
        engine,
        profile,
        usage_date=DAY,
        repair=True,
        empty_proof=proof,
        now=later,
        reader=FixtureReader(),
    )
    assert first["days"][0]["published"]
    second = runner.sync_daily(
        engine, profile, usage_date=DAY, repair=True, now=later, reader=FixtureReader()
    )
    assert second["days"][0]["published"]
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(daily).where(
                    daily.c.scope == "account_class", daily.c.resource_class == "tke_native"
                )
            )
            .mappings()
            .one()
        )
    assert row["metrics_json"]["cpu"]["coverage"] == 1
    assert "verified_empty" in row["metrics_json"]["cpu"]["quality"]


@pytest.mark.parametrize("unknown_request", [False, True])
def test_truncated_pod_series_preserves_consumer_integrals_even_with_unknown_requests(
    engine, profile, unknown_request
):
    evidence = [
        observation(end=NOW),
        observation(
            "pod",
            "pod-a",
            begin=START,
            end=NOW,
            node_name="np-one",
            cpu_request=2,
            memory_request=2_000_000,
        ),
        observation(
            "pod",
            "pod-b",
            begin=START,
            end=NOW,
            node_name="np-one",
            cpu_request=None if unknown_request else 2,
            memory_request=2_000_000,
        ),
    ]
    store.save_observations(engine, evidence, now=NOW)

    class Pods(FixtureReader):
        def __init__(self, truncate=False):
            super().__init__()
            self.truncate = truncate

        def window(self, start, end, observations):
            result = [
                point(stamp=start),
                point(metric="memory", stamp=start),
                point("pod", "cls-9zpon9y1|jenkins-tidb|pod-a", 1, stamp=start),
            ]
            if not self.truncate:
                result.append(point("pod", "cls-9zpon9y1|jenkins-tidb|pod-b", 1, stamp=start))
            return result

    assert runner.sync_daily(engine, profile, now=NOW, reader=Pods())["days"][-1]["published"]
    with engine.connect() as connection:
        before = (
            connection.execute(
                select(daily.c.metrics_json).where(daily.c.scope.in_(["job", "repo"]))
            )
            .scalars()
            .all()
        )
    result = runner.sync_daily(engine, profile, now=NOW, reader=Pods(truncate=True))
    assert not result["days"][-1]["published"]
    assert "consumer evidence regressed" in result["days"][-1]["reason"]
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(daily.c.metrics_json).where(daily.c.scope.in_(["job", "repo"]))
            )
            .scalars()
            .all()
            == before
        )


def test_partial_bucket_peak_is_separate_and_merges_to_weekly_without_becoming_full_peak(
    engine, profile
):
    evidence = [
        observation(entity="np-a", end=START + timedelta(minutes=2)),
        observation(entity="np-b", end=START + timedelta(minutes=2)),
    ]
    points = [
        point(entity="np-a", value=90),
        point(entity="np-a", value=20, stamp=START + timedelta(minutes=1)),
        point(entity="np-b", value=20, stamp=START + timedelta(minutes=1)),
    ]
    rows = aggregate(profile, points, evidence, minutes=2)
    body = metric(rows)
    assert body["peak"] == 20 and body["peak_coverage"] == 1
    assert body["peak_subset"] == 90 and body["peak_subset_coverage"] == 0.5
    target = {}
    runner.merge_window_rows(target, aggregate(profile, points, evidence))
    runner.merge_window_rows(
        target,
        aggregate(profile, points, evidence, minutes=2, window_start=START + timedelta(minutes=1)),
    )
    assert metric(list(target.values())) == body
    assert store.replace_day(engine, DAY, rows, now=NOW)["published"]
    from cost_insight.utilization.schema import weekly

    with engine.connect() as connection:
        summary = connection.execute(
            select(weekly.c.values_json).where(
                weekly.c.resource_class == "tke_native", weekly.c.metric == "cpu"
            )
        ).scalar_one()
    assert summary["peak"] == 20 and summary["peak_subset"] == 90
    assert summary["peak_subset_coverage"] == 0.5


def test_inventory_chunks_before_guarded_sink_cap(engine, profile, monkeypatch):
    reader = Mock()
    reader.stats = {"calls": 0}
    reader.inventory.return_value = [
        observation("cvm", f"ins-{index}", begin=NOW, end=NOW) for index in range(1001)
    ]
    sizes = []

    def save(_engine, values, **kwargs):
        sizes.append(len(values))
        return {"rows": len(values)}

    monkeypatch.setattr(store, "save_observations", save)
    result = runner.sync_inventory(engine, profile, now=NOW, reader=reader)
    assert sizes == [1000, 1]
    assert result["inventory"]["rows"] == 1001 and result["inventory"]["batches"] == 2


def test_foreign_account_rows_do_not_own_counter_or_block_target_expiry(engine):
    with engine.begin() as connection:
        foreign = dict(connection.execute(select(observations)).mappings().one())
        foreign.pop("id")
        foreign.update(
            vendor="other",
            account_id="another-account",
            week_start=DAY - timedelta(days=28),
            expires_at=(START - timedelta(days=14)).replace(tzinfo=None),
        )
        connection.execute(observations.insert().values(**foreign))
    assert store.maintain(engine, dry_run=True, now=NOW)["retained_rows"] == 1
    result = store.maintain(engine, now=policy.expires_at(DAY) + timedelta(hours=1))
    assert result["deleted_rows"] == 1 and result["state"]["retained_rows"] == 0
    assert result["state"]["oldest_expired_at"] is None and not result["state"]["paused"]
    with engine.connect() as connection:
        assert connection.execute(select(observations.c.account_id)).scalars().all() == [
            "another-account"
        ]
