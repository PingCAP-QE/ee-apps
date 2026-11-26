"""Small offline V2 end-to-end planner -> Kafka reference -> result tests."""
# ruff: noqa: DTZ001

import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select, text

from cost_insight.jobs import cli
from cost_insight.utilization import policy, runner, store, tencent, transport
from cost_insight.utilization.schema import machines, metadata, tasks

NOW = datetime(2026, 11, 25, 4, 30)


@pytest.fixture
def configured(tmp_path):
    raw = json.loads(
        (Path(__file__).parents[1] / "config/tencent-utilization.example.json").read_text()
    )
    raw["measurement"].update(
        verified_horizon_seconds=14 * 86400,
        safety_margin_seconds=3600,
        timestamp_alignment_verified=True,
        horizon_verified_at="2026-11-25T00:00:00+00:00",
        evidence_reference="offline-only",
        source_scales={kind: {"cpu": 1, "memory": 1} for kind in ("cvm", "tke_native", "pod")},
    )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw))
    profile = tencent.load_profile(str(path))
    engine = create_engine(f"sqlite:///{tmp_path}/pipeline.db")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE cost_job_state (job_name VARCHAR(255) PRIMARY KEY,watermark_json JSON,last_status TEXT,updated_at DATETIME)"
            )
        )
    store.configure(
        engine, profile, start_date=NOW.date() - timedelta(days=10), enabled=True, now=NOW
    )
    store.maintain(engine, now=NOW)
    yield engine, profile, path
    engine.dispose()


def candidate(day):
    start = policy.day_start(day)
    return {
        "machine_id": "ins-one",
        "region": "ap-beijing",
        "cluster_id": "",
        "resource_class": "cvm",
        "instance_type": "SA9.MEDIUM4",
        "cpu_cores": 2,
        "memory_bytes": 4 * 1024**3,
        "observed_from": start,
        "observed_until": start + timedelta(minutes=30),
    }


def test_historical_plan_dispatch_consume_is_idempotent(configured):
    engine, profile, _ = configured
    day = NOW.date() - timedelta(days=5)
    reader = Mock(stats={"calls": 1})
    reader.billing_candidates.return_value = [candidate(day)]
    for expected in (1, 0):
        result = runner.plan_machines(engine, profile, bill_day=day, now=NOW, reader=reader)
        assert result["days"][0]["planned"] == expected
    producer = Mock()
    assert runner.dispatch(engine, profile, producer, now=NOW)["sent"] == 1
    message = producer.publish.call_args.args[0]
    start = policy.day_start(day)
    reader.window.return_value = [
        {
            "entity": "ins-one",
            "metric": metric,
            "timestamp": start + timedelta(minutes=i),
            "value": 25,
            "unit": "percent",
        }
        for i in range(30)
        for metric in ("cpu", "memory")
    ]
    consumer = Mock()
    consumer.poll.side_effect = [{0: [Mock(value=json.dumps(message).encode())]}, {}]
    assert runner.consume(engine, profile, consumer, reader=reader, now=NOW)["done"] == 1
    consumer.commit.assert_called_once()
    assert runner.work_one(engine, profile, message["task_id"], now=NOW, reader=reader) == "ignored"
    with engine.connect() as c:
        row = c.execute(select(machines)).mappings().one()
        assert row["machine_id"] == "ins-one" and row["usage_date"] == day
        assert row["cpu_used"] / row["cpu_capacity"] == 0.25
        assert row["cpu_peak"] == 25
    # Query tasks are short-lived; their expiry never deletes a machine fact.
    store.maintain(engine, now=NOW + timedelta(days=20))
    with engine.connect() as c:
        assert c.execute(select(machines)).first() is not None
        assert c.execute(select(tasks)).first() is None


def test_late_bill_partition_enqueues_prior_usage_day_and_replay_is_idempotent(configured):
    engine, profile, _ = configured
    bill_day = NOW.date() - timedelta(days=3)
    usage_day = bill_day - timedelta(days=1)
    midnight = policy.day_start(bill_day)
    reader = Mock(stats={})
    fee = {
        **candidate(usage_day),
        "observed_from": midnight - timedelta(minutes=10),
        "observed_until": midnight + timedelta(minutes=10),
    }
    reader.billing_candidates.side_effect = lambda day: [fee] if day == bill_day else []

    for expected in (2, 0):
        result = runner.plan_machines(engine, profile, now=NOW, reader=reader)
        assert sum(day["planned"] for day in result["days"]) == expected
    with engine.connect() as connection:
        assert connection.execute(
            select(tasks.c.window_start, tasks.c.window_end).order_by(tasks.c.window_start)
        ).all() == [
            (midnight - timedelta(minutes=10), midnight),
            (midnight, midnight + timedelta(minutes=10)),
        ]


@pytest.mark.parametrize("revision", ["interval_extension", "overlap", "capacity_change"])
def test_machine_bill_revision_deduplicates_unchanged_windows_and_isolates_conflicts(
    configured, revision
):
    engine, profile, _ = configured
    reader = Mock(stats={})
    reader.billing_candidates.side_effect = lambda day: [candidate(day)]
    assert all(
        day["planned"] == 1
        for day in runner.plan_machines(engine, profile, now=NOW, reader=reader)["days"]
    )

    def revised_candidates(day):
        original = candidate(day)
        revised = {**original}
        if revision == "interval_extension":
            revised["observed_until"] += timedelta(minutes=30)
        elif revision == "overlap":
            revised["observed_from"] += timedelta(minutes=1)
            revised["observed_until"] -= timedelta(minutes=1)
        else:
            revised["cpu_cores"] = 8
        return [revised, {**original, "machine_id": "ins-unaffected"}]

    if revision == "interval_extension":
        # Previously planned tasks may still carry the enclosing bill interval.
        with engine.begin() as connection:
            for task in connection.execute(select(tasks)).mappings().all():
                payload = {
                    **task["payload_json"],
                    "observed_from": str(task["window_start"]),
                    "observed_until": str(task["window_end"]),
                }
                connection.execute(
                    tasks.update().where(tasks.c.id == task["id"]).values(payload_json=payload)
                )
    reader.billing_candidates.side_effect = revised_candidates
    result = runner.plan_machines(engine, profile, now=NOW, reader=reader)
    assert len(result["days"]) == 2
    for day in result["days"]:
        if revision == "interval_extension":
            assert day["planned"] == 2 and not day["errors"]
        else:
            assert day["planned"] == 1 and len(day["errors"]) == 1
            error = day["errors"][0]
            assert error["machine_id"] == "ins-one" and error["region"] == "ap-beijing"
            expected_error = "overlapping" if revision == "overlap" else "conflicting snapshot"
            assert expected_error in error["error"]
    with engine.connect() as connection:
        rows = connection.execute(select(tasks)).mappings().all()
        assert len(rows) == (6 if revision == "interval_extension" else 4)
        tails = [row for row in rows if row["window_start"].minute == 30]
        assert len(tails) == (2 if revision == "interval_extension" else 0)
        assert all(row["payload_json"]["machine_id"] == "ins-one" for row in tails)
        assert all(row["window_end"] - row["window_start"] == timedelta(minutes=30) for row in rows)


def test_ready_compatible_machine_tasks_share_one_monitor_read(configured):
    engine, profile, _ = configured
    day = NOW.date() - timedelta(days=5)
    reader = Mock(stats={})
    first = candidate(day)
    reader.billing_candidates.return_value = [first, {**first, "machine_id": "ins-two"}]
    runner.plan_machines(engine, profile, bill_day=day, now=NOW, reader=reader)
    with engine.connect() as connection:
        ids = connection.execute(select(tasks.c.id)).scalars().all()
    start = policy.day_start(day)
    reader.window.return_value = [
        {"entity": entity, "metric": metric, "timestamp": start, "value": 25, "unit": "percent"}
        for entity in ("ins-one", "ins-two")
        for metric in ("cpu", "memory")
    ]
    assert runner.work_batch(engine, profile, ids, now=NOW, reader=reader) == ["done", "done"]
    reader.window.assert_called_once()
    assert len(reader.window.call_args.args[2]) == 2
    with engine.connect() as connection:
        assert len(connection.execute(select(machines)).all()) == 2


def test_unresolved_pod_selector_does_not_retry_healthy_pods_in_same_poll(configured):
    engine, profile, _ = configured
    start, end = NOW - timedelta(minutes=30), NOW - timedelta(minutes=20)
    unresolved = {
        "kind": "pod", "region": profile["cluster_region"],
        "cluster_id": profile["cluster_id"], "namespace": profile["namespaces"][0],
        "pod_uid": "uid-unresolved", "pod_name": "pending-pod",
        "node_query_kind": "", "node_query_value": "",
        "cpu_request_cores": 1, "memory_request_bytes": 1024, "labels_json": {},
    }
    healthy = {
        **unresolved, "pod_uid": "uid-healthy", "pod_name": "running-pod",
        "node_query_kind": "node", "node_query_value": "10.0.0.10",
    }
    store.enqueue_windows(
        engine, [(target, start, end) for target in (unresolved, healthy)], now=NOW
    )
    with engine.connect() as connection:
        ids = {
            row["payload_json"]["pod_uid"]: row["id"]
            for row in connection.execute(select(tasks)).mappings()
        }
    source = tencent.TencentReader(profile, call=Mock(side_effect=AssertionError("no cloud calls")))

    def query_window(start, end, targets):
        for target in targets:
            source._target(target)  # The real source rejects unresolved watcher selectors.
        return [
            {"entity": target["pod_uid"], "metric": metric, "timestamp": start,
             "value": value, "unit": "absolute"}
            for target in targets for metric, value in (("cpu", 1), ("memory", 1024))
        ]

    reader = Mock()
    reader.window.side_effect = query_window
    assert runner.work_batch(
        engine, profile, [ids["uid-unresolved"], ids["uid-healthy"]], now=NOW, reader=reader
    ) == ["retry", "done"]
    with engine.connect() as connection:
        statuses = {
            row["payload_json"]["pod_uid"]: row["status"]
            for row in connection.execute(select(tasks)).mappings()
        }
        assert statuses == {"uid-unresolved": "retry", "uid-healthy": "done"}


def test_oversized_multi_partition_poll_is_chunked_before_ack(configured):
    engine, profile, _ = configured
    day = NOW.date() - timedelta(days=5)
    reader = Mock(stats={})
    reader.billing_candidates.return_value = [
        {**candidate(day), "machine_id": f"ins-{index}"} for index in range(23)
    ]
    runner.plan_machines(engine, profile, bill_day=day, now=NOW, reader=reader)
    with engine.connect() as connection:
        ids = connection.execute(select(tasks.c.id)).scalars().all()
    start = policy.day_start(day)
    reader.window.return_value = [
        {"entity": f"ins-{index}", "metric": metric, "timestamp": start,
         "value": 25, "unit": "percent"}
        for index in range(23) for metric in ("cpu", "memory")
    ]
    consumer = Mock()
    consumer.poll.side_effect = [{
        partition: [Mock(value={"task_id": key}) for key in ids[partition::3]]
        for partition in range(3)
    }]

    def check_durable_before_ack():
        with engine.connect() as connection:
            assert set(connection.execute(select(tasks.c.status)).scalars()) == {"done"}
            assert len(connection.execute(select(machines)).all()) == 23

    consumer.commit.side_effect = check_durable_before_ack
    result = runner.consume(engine, profile, consumer, max_messages=10, now=NOW, reader=reader)
    assert result["processed"] == result["done"] == 23
    assert [len(call.args[2]) for call in reader.window.call_args_list] == [10, 10, 3]
    consumer.commit.assert_called_once()


@pytest.mark.parametrize("rejection", ["points", "result", "contract"])
def test_per_task_rejection_persists_retry_and_does_not_block_other_tasks(configured, monkeypatch, rejection):
    engine, profile, _ = configured
    day = NOW.date() - timedelta(days=5)
    reader = Mock(stats={})
    first = candidate(day)
    reader.billing_candidates.return_value = [first, {**first, "machine_id": "ins-two"}]
    runner.plan_machines(engine, profile, bill_day=day, now=NOW, reader=reader)
    with engine.connect() as connection:
        rows = connection.execute(select(tasks)).mappings().all()
    if rejection == "contract":
        rejected = next(row for row in rows if row["payload_json"]["machine_id"] == "ins-one")
        with engine.begin() as connection:
            connection.execute(tasks.update().where(tasks.c.id == rejected["id"]).values(
                payload_json={**rejected["payload_json"], "contract_version": "obsolete"}
            ))
    if rejection == "result":
        summarize = runner.summarize_window

        def invalid_result(points, target, start, end):
            values = summarize(points, target, start, end)
            if target["machine_id"] == "ins-one":
                values["cpu_used"] = -1
            return values

        monkeypatch.setattr(runner, "summarize_window", invalid_result)
    start = policy.day_start(day)
    reader.window.return_value = [
        {"entity": entity, "metric": "cpu", "timestamp": start,
         "value": -1 if rejection == "points" and entity == "ins-one" else 25,
         "unit": "percent"}
        for entity in ("ins-one", "ins-two")
    ]
    consumer = Mock()
    consumer.poll.side_effect = [{0: [Mock(value={"task_id": row["id"]}) for row in rows]}, {}]

    def check_durable_before_ack():
        with engine.connect() as connection:
            assert sorted(connection.execute(select(tasks.c.status)).scalars()) == ["done", "retry"]
            assert connection.execute(select(machines.c.machine_id)).scalar_one() == "ins-two"

    consumer.commit.side_effect = check_durable_before_ack
    result = runner.consume(engine, profile, consumer, now=NOW, reader=reader)
    assert result["done"] == result["retry"] == 1
    consumer.commit.assert_called_once()


def test_database_failure_prevents_ack(configured, monkeypatch):
    engine, profile, _ = configured
    reader = Mock(stats={})
    day = NOW.date() - timedelta(days=5)
    reader.billing_candidates.return_value = [candidate(day)]
    runner.plan_machines(engine, profile, bill_day=day, now=NOW, reader=reader)
    with engine.connect() as connection:
        task_id = connection.execute(select(tasks.c.id)).scalar_one()
    reader.window.return_value = [{"entity": "ins-one", "metric": "cpu",
                                  "timestamp": NOW, "value": 25, "unit": "percent"}]
    monkeypatch.setattr(store, "complete_task", Mock(side_effect=RuntimeError("database down")))
    consumer = Mock()
    consumer.poll.return_value = {0: [Mock(value={"task_id": task_id})]}
    with pytest.raises(RuntimeError, match="database down"):
        runner.consume(engine, profile, consumer, now=NOW, reader=reader)
    consumer.commit.assert_not_called()


@pytest.mark.parametrize("kind,metric", [("cvm", "cpu"), ("tke_native", "memory")])
def test_planner_reports_exhausted_usage_day_after_bill_scan(configured, kind, metric):
    engine, profile, _ = configured
    state = store.load_state(engine)
    state["contracts"][kind][metric]["verified_horizon_seconds"] = 2 * 86400
    profile["contracts"] = state["contracts"]
    with engine.begin() as connection:
        store._save(connection, state)
    reader = Mock(stats={})
    partition = NOW.date() - timedelta(days=1)
    usage_day = NOW.date() - timedelta(days=3)
    reader.billing_candidates.return_value = [{**candidate(usage_day), "resource_class": kind}]
    result = runner.plan_machines(engine, profile, bill_day=partition, now=NOW, reader=reader)
    reader.billing_candidates.assert_called_once_with(partition)
    assert result["days"] == [{"usage_date": str(usage_day), "candidates": 1,
                               "horizon_gaps": 1, "planned": 0, "errors": []}]
    with engine.connect() as connection:
        assert connection.execute(select(tasks)).first() is None


def test_configuration_immutable_disable_and_dryrun(configured):
    engine, profile, _ = configured
    result = store.configure(
        engine, profile, start_date=NOW.date() - timedelta(days=10), dry_run=True
    )
    assert result["dry_run"]
    state = store.load_state(engine)
    assert state["contract_version"] == profile["contract_version"]
    assert "expired_tasks" not in state
    assert (
        store.configure(
            engine, profile, start_date=NOW.date() - timedelta(days=10), enabled=False, now=NOW
        )["enabled"]
        is False
    )
    with pytest.raises(ValueError, match="incompatible"):
        store.configure(engine, profile, start_date=NOW.date() - timedelta(days=9), now=NOW)


def test_empty_source_retries_and_invalid_reference_is_acked(configured):
    engine, profile, _ = configured
    reader = Mock(stats={})
    reader.billing_candidates.return_value = [candidate(NOW.date() - timedelta(days=3))]
    runner.plan_machines(engine, profile, now=NOW, reader=reader)
    with engine.connect() as c:
        task = c.execute(select(tasks)).mappings().first()
    reader.window.return_value = []
    assert runner.work_one(engine, profile, task["id"], now=NOW, reader=reader) == "retry"
    consumer = Mock()
    consumer.poll.side_effect = [{0: [Mock(value=b"not-json")]}, {}]
    assert runner.consume(engine, profile, consumer, now=NOW, reader=reader)["invalid"] == 1
    consumer.commit.assert_called_once()


def test_cli_dryrun_does_not_construct_kafka(configured, monkeypatch):
    engine, profile, path = configured
    monkeypatch.setenv("COST_INSIGHT_DB_URL", "sqlite:///:memory:")
    monkeypatch.setattr(cli, "build_engine", lambda settings: engine)
    monkeypatch.setattr(tencent, "load_profile", lambda path: profile)
    monkeypatch.setattr(transport, "Producer", Mock(side_effect=AssertionError("must not connect")))
    assert cli.main(["utilization-dispatch", "--profile", str(path), "--dry-run"]) == 0
    args = cli.build_parser().parse_args(
        ["utilization-worker", "--profile", str(path), "--limit", "2"]
    )
    assert args.limit == 2


@pytest.mark.parametrize("option", ["--bill-day", "--date"])
@pytest.mark.parametrize("command", ["sync-tencent-utilization", "utilization-probe"])
def test_cli_selects_billing_partition(command, option):
    args = cli.build_parser().parse_args([command, "--profile", "test.json", option, "2026-11-01"])
    day = date(2026, 11, 1)
    if command == "utilization-probe":
        assert args.bill_day == day
    else:
        assert args.bill_days == [day]


@pytest.mark.parametrize(
    "day_result,expected",
    [
        ({"candidates": 1, "planned": 1, "errors": []}, 0),
        ({"horizon_gaps": 1}, 2),
        ({"errors": [{"machine_id": "ins-one", "error": "conflicting snapshot"}]}, 2),
        ({"errors": [{"machine_id": "ins-one", "error": "utilization disabled/unhealthy"}]}, 2),
    ],
)
def test_cli_machine_planning_signals_incomplete_days(configured, monkeypatch, day_result, expected):
    engine, profile, path = configured
    monkeypatch.setenv("COST_INSIGHT_DB_URL", "sqlite:///:memory:")
    monkeypatch.setattr(cli, "build_engine", lambda settings: engine)
    monkeypatch.setattr(tencent, "load_profile", lambda path: profile)
    monkeypatch.setattr(
        runner,
        "plan_machines",
        Mock(return_value={
            "days": [
                {"usage_date": "2026-11-21", **day_result},
                {"usage_date": "2026-11-22", "candidates": 0, "planned": 0, "errors": []},
            ],
            "source": {},
            "dry_run": False,
        }),
    )
    assert cli.main(["sync-tencent-utilization", "--profile", str(path)]) == expected
    runner.plan_machines.assert_called_once_with(engine, profile, dry_run=False, bill_days=None)


def test_cli_replays_all_deferred_bill_days_together(configured, monkeypatch, capsys):
    engine, profile, path = configured
    monkeypatch.setenv("COST_INSIGHT_DB_URL", "sqlite:///:memory:")
    monkeypatch.setattr(cli, "build_engine", lambda settings: engine)
    monkeypatch.setattr(tencent, "load_profile", lambda path: profile)
    result = {"days": [], "deferred": [{
        "start": "2026-11-24T22:20:00+00:00",
        "end": "2026-11-26T23:00:00+00:00",
        "bill_days": ["2026-11-21", "2026-11-22"],
    }]}
    plan = Mock(return_value=result)
    monkeypatch.setattr(runner, "plan_machines", plan)

    assert cli.main([
        "sync-tencent-utilization", "--profile", str(path),
        "--bill-day", "2026-11-21", "--date", "2026-11-22",
    ]) == 2
    plan.assert_called_once_with(
        engine, profile, dry_run=False, bill_days=[date(2026, 11, 21), date(2026, 11, 22)]
    )
    assert json.loads(capsys.readouterr().out) == result


def test_kafka_transport_requires_tls_and_no_auto_create(monkeypatch):
    for name in (
        "UTILIZATION_KAFKA_BOOTSTRAP_SERVERS",
        "UTILIZATION_KAFKA_USERNAME",
        "UTILIZATION_KAFKA_PASSWORD",
        "UTILIZATION_KAFKA_CA_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError):
        transport._config()
    for key, value in {
        "UTILIZATION_KAFKA_BOOTSTRAP_SERVERS": "cluster-cd-kafka-bootstrap.kafka.svc:9094",
        "UTILIZATION_KAFKA_USERNAME": "offline",
        "UTILIZATION_KAFKA_PASSWORD": "offline-not-a-secret",
        "UTILIZATION_KAFKA_CA_FILE": "/tmp/offline-ca.crt",
    }.items():
        monkeypatch.setenv(key, value)
    assert transport._config()["security_protocol"] == "SASL_SSL"
    import sys

    fake = Mock()
    monkeypatch.setitem(sys.modules, "kafka", fake)
    producer = transport.Producer()
    producer.publish({"task_id": "a" * 64})
    fake.KafkaProducer.return_value.send.return_value.get.assert_called_once_with(timeout=20)
    producer.close()
    transport.consumer()
    assert fake.KafkaConsumer.call_args.kwargs["enable_auto_commit"] is False


def test_renderer_suspended_tencent_jobs_only():
    path = Path(__file__).parents[1] / "scripts/render-utilization-cronjobs.py"
    spec = importlib.util.spec_from_file_location("renderer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.render(
        "ee-cd", "ghcr.io/example/image:offline", "db", "cloud", "profile", "kafka", "kafka-ca"
    )
    assert len(result["items"]) == 4
    for job in result["items"]:
        assert job["spec"]["suspend"] and job["metadata"]["namespace"] == "ee-cd"
        assert job["spec"]["concurrencyPolicy"] == "Forbid"
    assert "inventory" not in json.dumps(result)
