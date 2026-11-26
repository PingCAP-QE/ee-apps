"""Offline V2 crash, idempotency, lease, quality and active-workset regression tests."""
# ruff: noqa: DTZ001
# The persistence contract deliberately uses UTC-naive business timestamps.

from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, select, text

from cost_insight.utilization import policy, runner, store
from cost_insight.utilization.aggregate import identity, summarize_window
from cost_insight.utilization.schema import contexts, machines, metadata, tasks, workloads

NOW = datetime(2026, 11, 25, 4, 30)
START = datetime(2026, 11, 25, 4)
END = START + timedelta(minutes=10)


def profile():
    contracts = {}
    for kind in ("cvm", "tke_native", "pod"):
        contracts[kind] = {
            metric: {
                "contract": {
                    "namespace": "QCE/CVM" if kind == "cvm" else "QCE/TKE2",
                    "metric": metric,
                    "period": 60,
                    "source_stat": "avg",
                    "normalized_unit": "cores" if metric == "cpu" else "bytes",
                    "denominator": "requests" if kind == "pod" else "physical",
                    "source_scale": 1,
                },
                "verified_horizon_seconds": 14 * 86400,
                "safety_margin_seconds": 3600,
                "timestamp_alignment_verified": True,
                "horizon_verified_at": "2026-11-25",
                "evidence_reference": "offline-test",
            }
            for metric in policy.METRICS
        }
    return {
        "account_id": policy.ACCOUNT_ID,
        "regions": ["ap-beijing"],
        "cluster_region": "ap-beijing",
        "cluster_id": "cls-test",
        "namespaces": ["ci"],
        "labels": ["test_project"],
        "contracts": contracts,
    }


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/v2.db")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE cost_job_state (job_name VARCHAR(255) PRIMARY KEY, watermark_json JSON, "
                "last_status VARCHAR(32),updated_at DATETIME)"
            )
        )
        state = {
            "version": 2,
            "enabled": True,
            "paused": False,
            "counts_reliable": True,
            "retained_rows": 0,
            "maintenance_at": NOW.isoformat(),
            "bytes_observed_at": NOW.isoformat(),
            "observed_bytes": 0,
            "contracts": profile()["contracts"],
            "scope": {
                k: profile()[k]
                for k in (
                    "account_id",
                    "regions",
                    "cluster_region",
                    "cluster_id",
                    "namespaces",
                    "labels",
                )
            },
            "start_date": "2026-11-01",
            "contract_version": identity(profile()["contracts"]),
        }
        import json

        connection.execute(
            text("INSERT INTO cost_job_state VALUES (:key,:state,'configured',CURRENT_TIMESTAMP)"),
            {"key": policy.GUARD_KEY, "state": json.dumps(state)},
        )
    yield engine
    engine.dispose()


def target(kind="machine", **changes):
    result = {
        "kind": kind,
        "account_id": policy.ACCOUNT_ID,
        "region": "ap-beijing",
        "cluster_id": "",
        "machine_id": "ins-test",
        "resource_class": "cvm",
        "instance_type": "T.MEDIUM4",
        "cpu_cores": 2.0,
        "memory_bytes": 4 * 1024**3,
    }
    if kind == "pod":
        result = {
            "kind": kind,
            "account_id": policy.ACCOUNT_ID,
            "region": "ap-beijing",
            "cluster_id": "cls-test",
            "namespace": "ci",
            "pod_uid": "uid-test",
            "pod_name": "test-1",
            "node_query_kind": "node",
            "node_query_value": "10.0.0.1",
            "cpu_request_cores": 4.0,
            "memory_request_bytes": 8 * 1024**3,
            "labels_json": {"test_project": "compatibility"},
        }
    return {**result, **changes}


def points(t, *, cpu=1, memory=1024):
    return [
        {
            "entity": t.get("machine_id") or t["pod_uid"],
            "metric": metric,
            "timestamp": START + timedelta(minutes=i),
            "value": value,
            "unit": "absolute",
        }
        for i in range(10)
        for metric, value in (("cpu", cpu), ("memory", memory))
    ]


def enqueued(engine, t=None):
    store.enqueue_windows(engine, [(t or target(), START, END)], now=NOW)
    with engine.connect() as connection:
        return connection.execute(select(tasks)).mappings().one()


def test_schema_contains_only_machine_days_generic_workload_active_context_and_tasks():
    assert set(metadata.tables) == {
        "machine_utilization_daily",
        "workload_utilization_daily",
        "pod_utilization_context",
        "utilization_task",
    }
    assert not {"repo", "job_name", "scope", "week_start", "vendor"} & set(machines.c.keys())
    assert not any("peak" in column.name for column in workloads.c)
    assert "pod_uid" not in workloads.c


def test_duplicate_enqueue_and_commit_are_exactly_once(engine):
    task = enqueued(engine)
    assert store.enqueue_windows(engine, [(target(), START, END)], now=NOW)["planned"] == 0
    claimed = store.claim_task(engine, task["id"], now=NOW)
    assert store.claim_task(engine, task["id"], now=NOW) is None
    result = summarize_window(points(target()), target(), START, END)
    assert store.complete_task(engine, task["id"], claimed["lease_token"], result, now=NOW)
    assert not store.complete_task(engine, task["id"], claimed["lease_token"], result, now=NOW)
    with engine.connect() as connection:
        fact = connection.execute(select(machines)).mappings().one()
        assert fact["cpu_used"] == 600 and fact["cpu_capacity"] == 1200
        assert fact["cpu_peak"] == 50
        assert connection.execute(select(tasks.c.status)).scalar_one() == "done"
    assert store.claim_task(engine, task["id"], now=NOW) is None


@pytest.mark.parametrize("explicit_cluster", [True, False])
def test_machine_window_extension_only_enqueues_unplanned_tail(engine, explicit_cluster):
    first_end = START + timedelta(minutes=15)
    t = target(observed_from=START, observed_until=first_end)
    if not explicit_cluster:
        t.pop("cluster_id")
    assert store.enqueue_windows(engine, [(t, START, first_end)], now=NOW) == {"planned": 1}
    (original,) = store.due_tasks(engine, now=NOW)

    clock = NOW + timedelta(minutes=10)
    revised = {**t, "observed_until": NOW}
    window = (revised, START, NOW)
    assert store.enqueue_windows(engine, [window], now=clock) == {"planned": 1}
    first, tail = sorted(store.due_tasks(engine, now=clock), key=lambda row: row["window_start"])
    assert first == original
    assert (tail["window_start"], tail["window_end"]) == (first_end, NOW)
    contract = first["payload_json"]["contract_version"]
    assert tail["payload_json"] == first["payload_json"]
    assert tail["id"] == identity(
        "machine",
        policy.ACCOUNT_ID,
        t["region"],
        t.get("cluster_id", ""),
        t["machine_id"],
        first_end.isoformat(),
        NOW.isoformat(),
        contract,
    )
    assert tail["deadline_at"] == first["deadline_at"] + timedelta(minutes=15)
    assert tail["due_at"] == clock
    assert store.enqueue_windows(engine, [window, (revised, first_end, NOW)], now=clock) == {
        "planned": 0
    }
    assert store.load_state(engine)["retained_rows"] == 2


def test_machine_window_extends_multiple_fragments_with_legacy_snapshot(engine):
    t = target()
    second_end = END + timedelta(minutes=10)
    # Insert in reverse order, then model an older checkpoint with saved bill bounds.
    assert store.enqueue_windows(engine, [(t, END, second_end), (t, START, END)], now=NOW) == {
        "planned": 2
    }
    first = min(store.due_tasks(engine, now=NOW), key=lambda row: row["window_start"])
    legacy_payload = {**first["payload_json"], "observed_from": str(START), "observed_until": str(END)}
    with engine.begin() as connection:
        connection.execute(
            tasks.update().where(tasks.c.id == first["id"]).values(payload_json=legacy_payload)
        )
    claim = store.claim_task(engine, first["id"], now=NOW)
    assert store.complete_task(
        engine, first["id"], claim["lease_token"], summarize_window([], t, START, END), now=NOW
    )

    revised = target(observed_from=START, observed_until=NOW)
    assert store.enqueue_windows(engine, [(revised, START, NOW)], now=NOW) == {"planned": 1}
    with engine.connect() as connection:
        rows = connection.execute(
            select(tasks.c.window_start, tasks.c.window_end, tasks.c.status).order_by(
                tasks.c.window_start
            )
        ).all()
    assert rows == [(START, END, "done"), (END, second_end, "pending"), (second_end, NOW, "pending")]
    windows = [(revised, START, NOW), (revised, START, second_end)]
    # Dry-run remains a candidate count, not a database-backed preview of inserts.
    assert store.enqueue_windows(engine, windows, now=NOW, dry_run=True) == {
        "dry_run": True,
        "planned": 2,
    }
    assert store.enqueue_windows(engine, windows, now=NOW) == {"planned": 0}
    assert store.load_state(engine)["retained_rows"] == 4  # three tasks and one fact


@pytest.mark.parametrize("conflict", ["shrink", "shift", "gap", "capacity", "contract", "pod"])
def test_window_overlap_without_equal_machine_prefix_rolls_back_batch(engine, conflict):
    t = target("pod" if conflict == "pod" else "machine")
    enqueued(engine, t)
    start = START + timedelta(minutes=1) if conflict == "shift" else START
    end = END - timedelta(minutes=1) if conflict == "shrink" else NOW
    if conflict in {"gap", "capacity", "contract"}:
        second_start = END + timedelta(minutes=1) if conflict == "gap" else END
        second_target = {**t, "cpu_cores": 4} if conflict == "capacity" else t
        second_end = END + timedelta(minutes=10)
        store.enqueue_windows(engine, [(second_target, second_start, second_end)], now=NOW)
        if conflict == "contract":
            with engine.begin() as connection:
                connection.execute(
                    tasks.update()
                    .where(tasks.c.window_start == second_start)
                    .values(payload_json={**t, "contract_version": "older"})
                )
    before = store.due_tasks(engine, now=NOW)
    # An earlier insert in the same candidate batch must not commit on a conflict.
    with pytest.raises(ValueError, match="overlapping task windows"):
        store.enqueue_windows(
            engine, [(target(machine_id="ins-other"), START, END), (t, start, end)], now=NOW
        )
    assert store.due_tasks(engine, now=NOW) == before
    assert store.load_state(engine)["retained_rows"] == len(before)


def test_lease_expiry_fences_old_worker(engine):
    task = enqueued(engine)
    first = store.claim_task(engine, task["id"], now=NOW)
    later = NOW + timedelta(minutes=6)
    second = store.claim_task(engine, task["id"], now=later)
    assert first["lease_token"] != second["lease_token"]
    result = summarize_window(points(target()), target(), START, END)
    assert not store.complete_task(engine, task["id"], first["lease_token"], result, now=later)
    assert store.complete_task(engine, task["id"], second["lease_token"], result, now=later)


def test_workload_labels_not_fixed_job_and_no_instance_persistence(engine):
    t = target("pod")
    task = enqueued(engine, t)
    claim = store.claim_task(engine, task["id"], now=NOW)
    result = summarize_window(points(t), t, START, END)
    store.complete_task(engine, task["id"], claim["lease_token"], result, now=NOW)
    with engine.connect() as connection:
        row = connection.execute(select(workloads)).mappings().one()
        assert row["labels_json"] == {"test_project": "compatibility"}
        assert row["cpu_used"] / row["cpu_capacity"] == 0.25
        assert connection.execute(select(machines)).first() is None


def test_failure_persisted_before_message_ack_and_retry(engine):
    task = enqueued(engine)
    reader = Mock()
    reader.window.side_effect = RuntimeError("upstream")
    consumer = Mock()
    consumer.poll.side_effect = [{0: [Mock(value={"task_id": task["id"]})]}, {}]
    result = runner.consume(engine, profile(), consumer, reader=reader, now=NOW)
    assert result["retry"] == 1
    consumer.commit.assert_called_once()
    with engine.connect() as connection:
        assert connection.execute(select(tasks.c.status)).scalar_one() == "retry"
        assert connection.execute(select(machines)).first() is None


def test_transient_failures_retry_beyond_five_attempts_then_recover(engine):
    task = enqueued(engine)
    reader = Mock()
    reader.window.side_effect = RuntimeError("upstream")
    clock = NOW
    for attempt in range(1, 13):
        assert runner.work_one(engine, profile(), task["id"], now=clock, reader=reader) == "retry"
        with engine.connect() as connection:
            row = connection.execute(select(tasks)).mappings().one()
            assert row["status"] == "retry" and row["attempts"] == attempt
            assert row["deadline_at"] == task["deadline_at"]
            assert row["due_at"] == clock + timedelta(seconds=min(300, 2**attempt))
            assert connection.execute(select(machines)).first() is None
        clock = row["due_at"]
    reader.window.side_effect = None
    reader.window.return_value = points(target())
    assert runner.work_one(engine, profile(), task["id"], now=clock, reader=reader) == "done"
    with engine.connect() as connection:
        row = connection.execute(select(machines)).mappings().one()
        assert row["cpu_observed_seconds"] == 600
        assert "collection_failed" not in row["quality_json"]


def test_retry_remains_open_until_deadline_then_maintenance_records_gap(engine):
    task = enqueued(engine)
    deadline = NOW + timedelta(seconds=20)
    with engine.begin() as connection:
        connection.execute(tasks.update().values(deadline_at=deadline))
    claim = store.claim_task(engine, task["id"], now=NOW)
    assert store.fail_task(engine, task["id"], claim["lease_token"], now=NOW)
    with engine.connect() as connection:
        assert connection.execute(select(tasks.c.status)).scalar_one() == "retry"
        assert connection.execute(select(machines)).first() is None
    store.maintain(engine, now=deadline)
    with engine.connect() as connection:
        row = connection.execute(select(machines)).mappings().one()
        assert row["cpu_observed_seconds"] == 0 and row["cpu_expected_seconds"] == 600
        assert "collection_failed" in row["quality_json"]
        assert connection.execute(select(tasks)).first() is None


def test_dispatch_crash_after_send_before_mark_replays_without_duplicate_fact(engine, monkeypatch):
    task = enqueued(engine)
    producer = Mock()
    real = store.mark_dispatched
    monkeypatch.setattr(store, "mark_dispatched", Mock(side_effect=RuntimeError("db-down")))
    with pytest.raises(RuntimeError):
        runner.dispatch(engine, profile(), producer, now=NOW)
    monkeypatch.setattr(store, "mark_dispatched", real)
    assert runner.dispatch(engine, profile(), producer, now=NOW)["sent"] == 1
    assert producer.publish.call_count == 2
    assert producer.publish.call_args.args[0] == {"task_id": task["id"]}


def test_context_short_pod_tail_planned_and_cleaned_after_done(engine):
    with engine.begin() as connection:
        t = target("pod")
        t.pop("kind")
        t.pop("region")
        connection.execute(
            contexts.insert().values(
                **t,
                context_key="a" * 64,
                observed_from=START,
                observed_until=START + timedelta(minutes=2),
                planned_until=START,
                closed_at=START + timedelta(minutes=2),
                expires_at=NOW + timedelta(hours=24),
            )
        )
    assert store.plan_contexts(engine, now=NOW)["planned"] == 1
    with engine.connect() as connection:
        task = connection.execute(select(tasks)).mappings().one()
    claim = store.claim_task(engine, task["id"], now=NOW)
    values = summarize_window([], target("pod"), task["window_start"], task["window_end"])
    store.complete_task(engine, task["id"], claim["lease_token"], values, now=NOW)
    store.maintain(engine, now=NOW)
    with engine.connect() as connection:
        assert connection.execute(select(contexts)).first() is None
        assert connection.execute(select(tasks)).first() is None
        assert connection.execute(select(workloads)).first() is not None
    assert store.claim_task(engine, task["id"], now=NOW) is None


@pytest.mark.parametrize("closed", [False, True])
def test_raw_watcher_clock_plans_only_whole_minutes_without_blocking_dispatch(engine, closed):
    enqueued(engine)
    observed = START + timedelta(minutes=23, seconds=47, microseconds=123456)
    end = START + timedelta(minutes=30)
    t = target("pod")
    t.pop("kind")
    t.pop("region")
    with engine.begin() as connection:
        connection.execute(
            contexts.insert().values(
                **t,
                context_key="b" * 64,
                observed_from=observed,
                observed_until=end + timedelta(seconds=25),
                planned_until=observed,
                closed_at=end + timedelta(seconds=25) if closed else None,
                expires_at=NOW + timedelta(hours=24),
            )
        )
    clock = NOW + timedelta(minutes=10)
    producer = Mock()
    assert runner.dispatch(engine, profile(), producer, now=clock) == {"planned": 1, "sent": 2}
    with engine.connect() as connection:
        pod_task = connection.execute(select(tasks).where(tasks.c.kind == "pod")).mappings().one()
        assert pod_task["window_start"] == START + timedelta(minutes=24)
        assert pod_task["window_end"] == end
        assert connection.execute(select(contexts.c.planned_until)).scalar_one() == end
    assert runner.dispatch(engine, profile(), producer, now=clock) == {"planned": 0, "sent": 0}


def test_sub_minute_context_never_enqueues_a_partial_minute(engine):
    observed = START + timedelta(seconds=47, microseconds=123456)
    t = target("pod")
    t.pop("kind")
    t.pop("region")
    with engine.begin() as connection:
        connection.execute(
            contexts.insert().values(
                **t,
                context_key="c" * 64,
                observed_from=observed,
                observed_until=START + timedelta(seconds=55),
                planned_until=observed,
                closed_at=START + timedelta(seconds=55),
                expires_at=NOW + timedelta(hours=24),
            )
        )
    assert store.plan_contexts(engine, now=NOW) == {"planned": 0}
    store.maintain(engine, now=NOW)
    with engine.connect() as connection:
        assert connection.execute(select(tasks)).first() is None
        assert connection.execute(select(contexts)).first() is None


@pytest.mark.parametrize("blocked", ["no_whole_minute", "open_boundary", "open_lag", "closed_lag"])
def test_ineligible_contexts_do_not_consume_dispatch_batch(engine, blocked):
    row = {key: value for key, value in target("pod").items() if key in contexts.c}
    clock = NOW + timedelta(minutes=6, seconds=5)
    if blocked == "no_whole_minute":
        first, last = START + timedelta(seconds=55), START + timedelta(minutes=1, seconds=5)
        closed = last
        ready_start, ready_end = START + timedelta(minutes=2), END
    elif blocked == "open_boundary":
        first, last = START, START + timedelta(minutes=9, seconds=59, microseconds=999999)
        closed = None
        ready_start, ready_end = START + timedelta(minutes=1), END
    else:
        # The next aligned window (or a closed tail not yet due) must not
        # take priority over a later confirmed tail whose reporting lag elapsed.
        first, last = START + timedelta(minutes=20), START + timedelta(minutes=29)
        if blocked == "open_lag":
            last += timedelta(minutes=1)
        closed = last if blocked == "closed_lag" else None
        ready_start, ready_end = START + timedelta(minutes=25), START + timedelta(minutes=26)
    expires = NOW + timedelta(hours=24)
    with engine.begin() as connection:
        connection.execute(contexts.insert(), [
            dict(row, pod_uid=f"uid-{i}", context_key=f"blocked-{i}",
                 observed_from=first, observed_until=last, planned_until=first,
                 closed_at=closed, expires_at=expires)
            for i in range(100)
        ])
        connection.execute(contexts.insert().values(
            **row, context_key="ready", observed_from=ready_start, observed_until=ready_end,
            planned_until=ready_start, closed_at=ready_end, expires_at=expires,
        ))
    producer = Mock()
    assert runner.dispatch(engine, profile(), producer, now=clock) == {"planned": 1, "sent": 1}
    store.maintain(engine, now=clock)
    with engine.connect() as connection:
        task = connection.execute(select(tasks)).mappings().one()
        assert task["payload_json"]["pod_uid"] == row["pod_uid"]
        if blocked == "no_whole_minute":
            assert connection.execute(select(contexts.c.pod_uid)).scalars().all() == [row["pod_uid"]]


@pytest.mark.parametrize("available", [0, 1])
def test_dispatch_uses_actual_row_headroom_and_still_sends_existing_work(
    engine, monkeypatch, available
):
    enqueued(engine)
    row = {key: value for key, value in target("pod").items() if key in contexts.c}
    with engine.begin() as connection:
        connection.execute(
            contexts.insert().values(
                **row,
                context_key="due",
                observed_from=START,
                observed_until=START + timedelta(minutes=20),
                planned_until=START,
                expires_at=NOW + timedelta(hours=24),
            )
        )
    store.maintain(engine, now=NOW)
    # One task plus one context; admit at most one more row, not a full batch.
    monkeypatch.setattr(policy, "ROW_LIMIT", 3 + available)
    producer = Mock()
    assert runner.dispatch(engine, profile(), producer, now=NOW) == {
        "planned": available,
        "sent": available + 1,
    }
    assert store.load_state(engine)["retained_rows"] == available + 2
    assert producer.publish.call_count == available + 1


def test_at_row_headroom_zero_existing_fact_can_still_be_updated(engine, monkeypatch):
    t = target()
    store.enqueue_windows(engine, [(t, START, END), (t, END, END + timedelta(minutes=10))], now=NOW)
    ordered = sorted(store.due_tasks(engine, now=NOW), key=lambda row: row["window_start"])
    for index, task in enumerate(ordered):
        if index:
            # Both tasks and the machine day already exist; the second result
            # updates that day without creating a fourth row.
            monkeypatch.setattr(policy, "ROW_LIMIT", 4)
        claim = store.claim_task(engine, task["id"], now=NOW)
        result = summarize_window([], t, task["window_start"], task["window_end"])
        assert store.complete_task(engine, task["id"], claim["lease_token"], result, now=NOW)
    with engine.connect() as connection:
        assert connection.execute(select(machines.c.cpu_expected_seconds)).scalar_one() == 1200
    assert store.load_state(engine)["retained_rows"] == 3


def test_machine_bill_conflict_survives_partition_roll_and_requires_joint_replay(engine):
    day = policy.usage_date(START)
    prior = day - timedelta(days=1)
    old = {**target(), "observed_from": START, "observed_until": END}
    corrected = {**old, "cpu_cores": 4}
    reader = Mock(stats={})
    snapshots = {prior: [old], day: [corrected]}
    reader.billing_candidates.side_effect = lambda part: snapshots[part]

    def plan(*days, now=NOW, dry_run=False):
        return runner.plan_machines(engine, profile(), bill_days=days, reader=reader,
                                    now=now, dry_run=dry_run)["days"][0]

    assert plan(prior, day, dry_run=True)["errors"]
    assert not store.load_state(engine).get("machine_billing_conflicts")
    assert plan(prior, day)["errors"]
    assert len(store.load_state(engine)["machine_billing_conflicts"]) == 1
    rolled = plan(day, now=NOW + timedelta(minutes=1))
    assert rolled["planned"] == 0 and "unresolved" in rolled["errors"][0]["error"]
    snapshots[prior] = [corrected]
    assert plan(prior, day, now=NOW + timedelta(minutes=2))["planned"] == 1
    assert not store.load_state(engine)["machine_billing_conflicts"]


def test_shifted_machine_window_cannot_reopen_a_day_after_checkpoint_purge(engine):
    start = policy.day_start(NOW.date() - timedelta(days=1))
    end = start + timedelta(minutes=30)
    t = target()
    store.enqueue_windows(engine, [(t, start, end)], now=NOW)
    task = store.due_tasks(engine, now=NOW)[0]
    claimed = store.claim_task(engine, task["id"], now=NOW)
    assert store.complete_task(
        engine, task["id"], claimed["lease_token"], summarize_window([], t, start, end), now=NOW
    )
    expired = task["deadline_at"]
    store.maintain(engine, now=expired)
    # A late bill shifts the start 15 minutes later. That individual window
    # could still be queried, but the prior overlapping checkpoint is gone.
    with pytest.raises(ValueError, match="usage day has expired"):
        store.enqueue_windows(engine, [(t, start + timedelta(minutes=15), end)], now=expired)
    with engine.connect() as connection:
        assert connection.execute(select(tasks)).first() is None
        assert connection.execute(select(machines.c.cpu_expected_seconds)).scalar_one() == 1800


def test_machine_prefix_does_not_reopen_usage_day_after_checkpoint_purge(engine):
    midnight = policy.day_start(NOW.date() - timedelta(days=1))
    start, middle, end = (midnight + timedelta(minutes=n) for n in (10, 15, 20))
    t = target()
    store.enqueue_windows(
        engine, [(t, midnight, start), (t, start, middle), (t, middle, end)], now=NOW
    )
    first = min(store.due_tasks(engine, now=NOW), key=lambda row: row["window_start"])
    clock = first["deadline_at"]
    store.maintain(engine, now=clock)
    retained = store.due_tasks(engine, now=clock)
    assert {row["window_start"] for row in retained} == {start, middle}

    assert store.enqueue_windows(engine, [(t, start, end)], now=clock) == {"planned": 0}
    with pytest.raises(ValueError, match="usage day has expired"):
        store.enqueue_windows(engine, [(t, start, end + timedelta(minutes=5))], now=clock)
    assert store.due_tasks(engine, now=clock) == retained
    assert store.load_state(engine)["retained_rows"] == 3  # two tasks and one fact


def test_expired_source_window_records_missing_before_task_purge(engine):
    enqueued(engine)
    later = NOW + timedelta(hours=1)
    with engine.begin() as connection:
        connection.execute(tasks.update().values(deadline_at=NOW - timedelta(seconds=1)))
    store.maintain(engine, now=later)
    with engine.connect() as connection:
        row = connection.execute(select(machines)).mappings().one()
        assert row["cpu_expected_seconds"] == 600 and row["cpu_observed_seconds"] == 0
        assert "collection_failed" in row["quality_json"]
        assert connection.execute(select(tasks)).first() is None


@pytest.mark.parametrize(
    "kind,purge_path", [("machine", "deadline"), ("pod", "deadline"), ("pod", "context")]
)
def test_maintenance_drains_expired_tasks_before_purging_their_population(engine, kind, purge_path):
    t = target(kind)
    task = enqueued(engine, t)
    claim = store.claim_task(engine, task["id"], now=NOW)
    store.complete_task(
        engine,
        task["id"],
        claim["lease_token"],
        summarize_window(points(t), t, START, END),
        now=NOW,
    )
    later = NOW + timedelta(hours=25)
    with engine.begin() as connection:
        connection.execute(tasks.delete())
        context_id = None
        if kind == "pod":
            context_id = connection.execute(
                contexts.insert().values(
                    **{k: v for k, v in t.items() if k not in {"kind", "region"}},
                    context_key="d" * 64,
                    observed_from=START,
                    observed_until=END + timedelta(minutes=30),
                    planned_until=END + timedelta(minutes=30),
                    closed_at=END + timedelta(minutes=30),
                    expires_at=NOW + timedelta(hours=24),
                )
            ).inserted_primary_key[0]
        # Deadline order differs from ID order in the generic purge case.
        ids = ["c", "b", "a"] if purge_path == "deadline" else ["a", "b", "c"]
        for i, (key, status) in enumerate(zip(ids, ("pending", "retry", "running"), strict=True)):
            connection.execute(
                tasks.insert().values(
                    **{
                        **task,
                        "id": key * 64,
                        "context_id": context_id,
                        "window_start": END + timedelta(minutes=10 * i),
                        "window_end": END + timedelta(minutes=10 * (i + 1)),
                        "deadline_at": NOW + timedelta(minutes=i),
                        "status": status,
                    }
                )
            )
    if purge_path == "deadline":
        store.maintain(engine, now=later, batch_size=2, max_rows=1)
        with engine.connect() as connection:
            remaining = connection.execute(
                select(tasks.c.status).where(tasks.c.id == "a" * 64)
            ).scalar_one()
            assert remaining == "running"
            if context_id is not None:
                assert connection.execute(select(contexts.c.id)).scalar_one() == context_id
    # The bounded expiration pass may leave work/contexts for the next run.
    store.maintain(engine, now=later, batch_size=2)
    store.maintain(engine, now=later, batch_size=2)
    with engine.connect() as connection:
        row = (
            connection.execute(select(machines if kind == "machine" else workloads))
            .mappings()
            .one()
        )
        for metric in policy.METRICS:
            assert row[f"{metric}_expected_seconds"] == 2400
            assert row[f"{metric}_observed_seconds"] == 600
        assert row["cpu_expected_capacity"] == 2400 * (2 if kind == "machine" else 4)
        assert row["memory_expected_capacity"] == 2400 * (4 if kind == "machine" else 8) * 1024**3
        assert "collection_failed" in row["quality_json"]
        assert connection.execute(select(tasks)).first() is None
        assert connection.execute(select(contexts)).first() is None


def test_live_context_still_plans_source_expired_windows_for_missing_accounting(engine):
    t = target("pod")
    observed = START - timedelta(days=2)
    with engine.begin() as connection:
        connection.execute(
            contexts.insert().values(
                **{k: v for k, v in t.items() if k not in {"kind", "region"}},
                context_key="3" * 64,
                observed_from=observed,
                observed_until=NOW,
                planned_until=observed,
                expires_at=NOW + timedelta(hours=24),
            )
        )
    assert store.plan_contexts(engine, now=NOW, limit=1) == {"planned": 1}
    with engine.connect() as connection:
        task = connection.execute(select(tasks)).mappings().one()
        assert task["deadline_at"] < NOW
        assert task["window_start"] == observed
        assert task["window_end"] == observed + timedelta(minutes=10)
    store.maintain(engine, now=NOW)
    with engine.connect() as connection:
        row = connection.execute(select(workloads)).mappings().one()
        assert row["cpu_expected_seconds"] == 600 and row["cpu_observed_seconds"] == 0
        assert (
            connection.execute(select(contexts.c.planned_until)).scalar_one() == task["window_end"]
        )
        assert connection.execute(select(tasks)).first() is None


@pytest.mark.parametrize("closed", [False, True])
def test_expired_unplanned_context_tail_lowers_prior_workload_coverage(engine, closed):
    t = target("pod", memory_request_bytes=float(8 * 1024**3))
    task = enqueued(engine, t)
    claim = store.claim_task(engine, task["id"], now=NOW)
    store.complete_task(
        engine,
        task["id"],
        claim["lease_token"],
        summarize_window(points(t), t, START, END),
        now=NOW,
    )
    tail_end = END + timedelta(minutes=20, seconds=47)
    later = NOW + timedelta(hours=25)
    with engine.begin() as connection:
        context_id = connection.execute(
            contexts.insert().values(
                **{k: v for k, v in t.items() if k not in {"kind", "region"}},
                context_key="e" * 64,
                observed_from=START,
                observed_until=tail_end,
                planned_until=END,
                closed_at=tail_end if closed else None,
                expires_at=NOW + timedelta(hours=24),
            )
        ).inserted_primary_key[0]
        connection.execute(tasks.update().values(context_id=context_id))
        state = store._state(connection)
        state.update(paused=True, pause_reasons=["cleanup_backlog"])
        store._save(connection, state)
    # Maintenance must repair this even when admission prevents dispatcher recovery.
    with pytest.raises(ValueError, match="unhealthy"):
        store.plan_contexts(engine, now=later)
    store.maintain(engine, now=later)
    with engine.connect() as connection:
        row = connection.execute(select(workloads)).mappings().one()
        for metric in policy.METRICS:
            assert row[f"{metric}_expected_seconds"] == 1800
            assert row[f"{metric}_observed_seconds"] == 600
            assert row[f"{metric}_usage_seconds"] == 600
            assert min(
                row[f"{metric}_capacity"] / row[f"{metric}_expected_capacity"],
                row[f"{metric}_observed_seconds"] / row[f"{metric}_expected_seconds"],
            ) == pytest.approx(1 / 3)
        assert row["cpu_expected_capacity"] == 1800 * 4
        assert row["memory_expected_capacity"] == 1800 * 8 * 1024**3
        assert row["cpu_used"] == 600
        assert "collection_failed" in row["quality_json"]
        assert row["cutoff"] == START + timedelta(minutes=30)
        assert connection.execute(select(contexts)).first() is None
        assert connection.execute(select(tasks)).first() is None


def test_expired_unplanned_tail_drains_in_bounded_atomic_day_windows(engine, monkeypatch):
    t = target("pod", cpu_request_cores=None, memory_request_bytes=0)
    observed = datetime(2026, 11, 25, 15, 57, 47)
    tail_end = datetime(2026, 11, 25, 16, 53, 25)
    later = tail_end + timedelta(hours=25)
    with engine.begin() as connection:
        connection.execute(
            contexts.insert().values(
                **{k: v for k, v in t.items() if k not in {"kind", "region"}},
                context_key="f" * 64,
                observed_from=observed,
                observed_until=tail_end,
                planned_until=observed,
                closed_at=tail_end,
                expires_at=tail_end + timedelta(hours=24),
            )
        )
    real_save = store._save
    monkeypatch.setattr(store, "_save", Mock(side_effect=RuntimeError("disconnect")))
    with pytest.raises(RuntimeError, match="disconnect"):
        store.maintain(engine, now=later, batch_size=2)
    with engine.connect() as connection:
        assert connection.execute(select(contexts.c.planned_until)).scalar_one() == observed
        assert connection.execute(select(workloads)).first() is None
    monkeypatch.setattr(store, "_save", real_save)

    windows = []
    real_apply = store._apply

    def record_apply(connection, task, values, clock):
        windows.append((task["window_start"], task["window_end"]))
        return real_apply(connection, task, values, clock)

    monkeypatch.setattr(store, "_apply", record_apply)
    # Seven ten-minute-aligned windows, including midnight and partial tails.
    for count, expected_cursor in (
        (2, datetime(2026, 11, 25, 16, 10)),
        (4, datetime(2026, 11, 25, 16, 30)),
        (6, datetime(2026, 11, 25, 16, 50)),
        (7, None),
    ):
        store.maintain(engine, now=later, batch_size=2)
        assert len(windows) == count
        with engine.connect() as connection:
            assert connection.execute(select(contexts.c.planned_until)).scalar() == expected_cursor
            rows = (
                connection.execute(select(workloads).order_by(workloads.c.usage_date))
                .mappings()
                .all()
            )
            total_seconds = sum(row["cpu_expected_seconds"] for row in rows)
            assert total_seconds == sum((end - start).total_seconds() for start, end in windows)
            assert connection.execute(select(tasks)).first() is None
    assert len(windows) == len(set(windows))
    assert windows[0][0] == datetime(2026, 11, 25, 15, 58)
    assert windows[-1][1] == datetime(2026, 11, 25, 16, 53)
    assert all(
        policy.usage_date(start) == policy.usage_date(end - timedelta(seconds=1))
        for start, end in windows
    )
    with engine.connect() as connection:
        rows = (
            connection.execute(select(workloads).order_by(workloads.c.usage_date)).mappings().all()
        )
        assert [row["cpu_expected_seconds"] for row in rows] == [120, 53 * 60]
        for row in rows:
            assert row["cpu_unknown_seconds"] == row["cpu_expected_seconds"]
            assert row["memory_zero_seconds"] == row["memory_expected_seconds"]
            assert row["cpu_observed_seconds"] == row["memory_observed_seconds"] == 0
    store.maintain(engine, now=later, batch_size=2)
    assert len(windows) == 7


def test_expired_tail_window_budget_is_shared_across_contexts(engine, monkeypatch):
    t = target("pod")
    with engine.begin() as connection:
        for i in range(2):
            connection.execute(
                contexts.insert().values(
                    **{k: v for k, v in t.items() if k not in {"kind", "region", "pod_uid"}},
                    pod_uid=f"uid-{i}",
                    context_key=str(i + 4) * 64,
                    observed_from=START,
                    observed_until=START + timedelta(minutes=30),
                    planned_until=START,
                    closed_at=START + timedelta(minutes=30),
                    expires_at=NOW + timedelta(hours=24),
                )
            )
    applied, batches = [], []
    real_apply = store._apply

    def record_apply(connection, task, values, clock):
        applied.append((task["payload_json"]["pod_uid"], task["window_start"]))
        return real_apply(connection, task, values, clock)

    def record_commit(connection):
        batches.append(len(applied) - sum(batches))

    monkeypatch.setattr(store, "_apply", record_apply)
    event.listen(engine, "commit", record_commit)
    try:
        for _ in range(3):
            store.maintain(engine, now=NOW + timedelta(hours=25), batch_size=2)
    finally:
        event.remove(engine, "commit", record_commit)
    assert max(batches) == 2
    assert len(applied) == len(set(applied)) == 6
    with engine.connect() as connection:
        assert connection.execute(select(contexts)).first() is None
        assert connection.execute(select(workloads.c.cpu_expected_seconds)).scalar_one() == 3600


def test_expired_tail_does_not_overlap_pending_task_expiry(engine):
    t = target("pod", memory_request_bytes=float(8 * 1024**3))
    task = enqueued(engine, t)
    tail_end = END + timedelta(minutes=20)
    later = NOW + timedelta(hours=25)
    with engine.begin() as connection:
        context_id = connection.execute(
            contexts.insert().values(
                **{k: v for k, v in t.items() if k not in {"kind", "region"}},
                context_key="1" * 64,
                observed_from=START,
                observed_until=tail_end,
                planned_until=END,
                closed_at=tail_end,
                expires_at=NOW + timedelta(hours=24),
            )
        ).inserted_primary_key[0]
        connection.execute(tasks.update().values(context_id=context_id))
    for _ in range(2):
        store.maintain(engine, now=later, batch_size=1)
    with engine.connect() as connection:
        row = connection.execute(select(workloads)).mappings().one()
        assert row["cpu_expected_seconds"] == row["memory_expected_seconds"] == 1800
        assert row["cpu_observed_seconds"] == 0
        assert connection.execute(select(contexts)).first() is None
        assert connection.execute(select(tasks).where(tasks.c.id == task["id"])).first() is None
    store.maintain(engine, now=later, batch_size=1)
    with engine.connect() as connection:
        assert connection.execute(select(workloads.c.cpu_expected_seconds)).scalar_one() == 1800


def test_expired_tail_skips_out_of_retention_workload_days(engine):
    t = target("pod")
    # Cross the oldest retained Beijing-week boundary: discard Sunday, keep Monday.
    retained_start = policy.day_start(policy.usage_date(NOW) - timedelta(days=9))
    observed = retained_start - timedelta(minutes=10)
    tail_end = retained_start + timedelta(minutes=10)
    with engine.begin() as connection:
        connection.execute(
            contexts.insert().values(
                **{k: v for k, v in t.items() if k not in {"kind", "region"}},
                context_key="2" * 64,
                observed_from=observed,
                observed_until=tail_end,
                planned_until=observed,
                closed_at=tail_end,
                expires_at=tail_end + timedelta(hours=24),
            )
        )
    store.maintain(engine, now=NOW, batch_size=1)
    with engine.connect() as connection:
        assert connection.execute(select(workloads)).first() is None
        assert connection.execute(select(contexts.c.planned_until)).scalar_one() == retained_start
    store.maintain(engine, now=NOW, batch_size=1)
    with engine.connect() as connection:
        row = connection.execute(select(workloads)).mappings().one()
        assert row["usage_date"] == policy.usage_date(retained_start)
        assert row["cpu_expected_seconds"] == row["memory_expected_seconds"] == 600
        assert row["cpu_observed_seconds"] == 0
        assert connection.execute(select(contexts)).first() is None


def test_maintenance_closes_orphan_contexts_in_keyset_batches(engine):
    t = target("pod")
    with engine.begin() as connection:
        for i in range(5):
            connection.execute(
                contexts.insert().values(
                    **{k: v for k, v in t.items() if k not in {"kind", "region"}},
                    context_key=str(i) * 64,
                    observed_from=START,
                    observed_until=END,
                    planned_until=START,
                    expires_at=NOW + timedelta(hours=24),
                )
            )
    batch_counts = []

    def record_updates(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE pod_utilization_context"):
            batch_counts.append(cursor.rowcount)

    event.listen(engine, "after_cursor_execute", record_updates)
    try:
        store.maintain(engine, now=NOW, batch_size=2)
    finally:
        event.remove(engine, "after_cursor_execute", record_updates)
    assert batch_counts == [2, 2, 1]
    with engine.connect() as connection:
        assert connection.execute(select(contexts.c.closed_at)).scalars().all() == [END] * 5


@pytest.mark.parametrize("budget", ["row", "byte"])
def test_maintenance_exposes_warning_tier_without_pausing(engine, monkeypatch, budget):
    enqueued(engine)
    monkeypatch.setattr(policy, "ROW_WARNING", 1 if budget == "row" else 50)
    monkeypatch.setattr(policy, "ROW_LIMIT", 2 if budget == "row" else 100)
    monkeypatch.setattr(policy, "BYTE_WARNING", 1 if budget == "byte" else 50)
    monkeypatch.setattr(policy, "BYTE_LIMIT", 2 if budget == "byte" else 100)
    monkeypatch.setattr(store, "_bytes", lambda connection: 1 if budget == "byte" else 0)
    result = store.maintain(engine, now=NOW)
    assert result["warnings"] == [budget + "_budget"]
    assert not result["paused"]


def test_budget_fail_closed_and_maintenance_initialization(engine):
    from cost_insight.jobs.state_store import checkpoint_job_watermark

    with engine.begin() as connection:
        state = store._state(connection)
        state.update(maintenance_at=None, paused=True)
        checkpoint_job_watermark(connection, policy.GUARD_KEY, state)
    with pytest.raises(ValueError, match="unhealthy"):
        enqueued(engine)
    assert store.maintain(engine, now=NOW)["paused"] is False
    assert enqueued(engine)


@pytest.mark.parametrize("budget", ["row", "byte"])
def test_maintenance_reports_budget_pause_and_keeps_recovery_hysteresis(engine, monkeypatch, budget):
    extra = enqueued(engine)
    if budget == "row":
        store.enqueue_windows(engine, [(target(machine_id="ins-other"), START, END)], now=NOW)
    monkeypatch.setattr(policy, "ROW_WARNING", 1 if budget == "row" else 50)
    monkeypatch.setattr(policy, "ROW_LIMIT", 2 if budget == "row" else 100)
    monkeypatch.setattr(policy, "BYTE_WARNING", 1 if budget == "byte" else 50)
    monkeypatch.setattr(policy, "BYTE_LIMIT", 2 if budget == "byte" else 100)
    monkeypatch.setattr(store, "_bytes", lambda connection: 2 if budget == "byte" else 0)
    assert store.maintain(engine, now=NOW)["paused"]
    assert store.load_state(engine)["pause_reasons"] == [budget + "_budget"]
    # Recovery still requires dropping below the warning threshold.
    if budget == "row":
        with engine.begin() as connection:
            connection.execute(tasks.delete().where(tasks.c.id == extra["id"]))
    else:
        monkeypatch.setattr(store, "_bytes", lambda connection: 1)
    assert store.maintain(engine, now=NOW)["paused"]
    assert store.load_state(engine)["pause_reasons"] == [budget + "_budget"]
    with engine.begin() as connection:
        connection.execute(tasks.delete())
    monkeypatch.setattr(store, "_bytes", lambda connection: 0)
    assert not store.maintain(engine, now=NOW)["paused"]
    assert store.load_state(engine)["pause_reasons"] == []


def test_maintenance_reports_remaining_expired_context_cleanup_backlog(engine):
    t = target("pod")
    t.pop("kind")
    t.pop("region")
    with engine.begin() as connection:
        for i in range(2):
            connection.execute(
                contexts.insert().values(
                    **{**t, "pod_uid": f"uid-{i}"},
                    context_key=str(i) * 64,
                    observed_from=START - timedelta(days=1),
                    observed_until=START - timedelta(days=1),
                    planned_until=START - timedelta(days=1),
                    closed_at=START - timedelta(days=1),
                    expires_at=NOW - timedelta(hours=5),
                )
            )
    assert store.maintain(engine, now=NOW, max_rows=1)["paused"]
    assert store.load_state(engine)["pause_reasons"] == ["cleanup_backlog"]
    assert not store.maintain(engine, now=NOW)["paused"]


@pytest.mark.parametrize("kind", ["machine", "workload"])
def test_maintenance_pauses_for_overdue_task_or_workload(engine, kind):
    if kind == "machine":
        enqueued(engine)
        store.enqueue_windows(engine, [(target(machine_id="ins-other"), START, END)], now=NOW)
        with engine.begin() as connection:
            connection.execute(tasks.update().values(deadline_at=NOW - timedelta(hours=5)))
    else:
        for i in range(2):
            t = target("pod", pod_uid=f"uid-{i}", labels_json={"test_project": f"proj-{i}"})
            store.enqueue_windows(engine, [(t, START, END)], now=NOW)
        for task in store.due_tasks(engine, now=NOW):
            claim = store.claim_task(engine, task["id"], now=NOW)
            assert store.complete_task(
                engine,
                task["id"],
                claim["lease_token"],
                summarize_window([], claim["payload_json"], START, END),
                now=NOW,
            )
        with engine.begin() as connection:
            connection.execute(workloads.update().values(expires_at=NOW - timedelta(hours=5)))
    assert store.maintain(engine, now=NOW, max_rows=1, batch_size=1)["paused"]
    assert store.load_state(engine)["pause_reasons"] == ["cleanup_backlog"]
    assert not store.maintain(engine, now=NOW)["paused"]


@pytest.mark.parametrize("failure", ["incomplete", "sql_error"])
def test_failed_byte_measurement_invalidates_previous_health(engine, monkeypatch, failure):
    assert not store.maintain(engine, now=NOW)["paused"]

    def reject_dbstat(connection, cursor, statement, parameters, *args):
        if "FROM dbstat" in statement:
            return "SELECT * FROM unavailable_stats", ()
        return statement, parameters

    with monkeypatch.context() as patch:
        if failure == "incomplete":
            patch.setattr(store, "_bytes", Mock(side_effect=ValueError("incomplete statistics")))
        else:
            event.listen(engine, "before_cursor_execute", reject_dbstat, retval=True)
        try:
            assert store.maintain(engine, now=NOW + timedelta(minutes=15))["paused"]
        finally:
            if failure == "sql_error":
                event.remove(engine, "before_cursor_execute", reject_dbstat)
        state = store.load_state(engine)
        assert state["pause_reasons"] == ["bytes_unreliable"]
        assert state["observed_bytes"] is None and state["bytes_observed_at"] is None
        with pytest.raises(ValueError, match="unhealthy"):
            store.enqueue_windows(engine, [(target(), START, END)], now=NOW + timedelta(minutes=16))
    assert not store.maintain(engine, now=NOW + timedelta(minutes=17))["paused"]


def test_dry_run_no_writes_and_account_scope(engine):
    assert (
        store.enqueue_windows(engine, [(target(), START, END)], now=NOW, dry_run=True)["planned"]
        == 1
    )
    with engine.connect() as connection:
        assert connection.execute(select(tasks)).first() is None
    with pytest.raises(ValueError, match="account"):
        enqueued(engine, target(account_id="other"))


def test_result_rolls_back_when_deadline_passes_at_final_clock_check(engine, monkeypatch):
    task = enqueued(engine)
    claim = store.claim_task(engine, task["id"], now=NOW)
    deadline = claim["lease_until"]
    with engine.begin() as connection:
        connection.execute(tasks.update().values(deadline_at=deadline))
    clock = Mock(side_effect=[deadline - timedelta(seconds=2),
                              deadline - timedelta(seconds=1), deadline])
    monkeypatch.setattr(store, "database_now", clock)
    values = summarize_window(points(target()), target(), START, END)
    with pytest.raises(ValueError, match="lease/deadline"):
        store.complete_task(engine, task["id"], claim["lease_token"], values)
    with engine.connect() as connection:
        assert connection.execute(select(machines)).first() is None
        assert connection.execute(select(tasks.c.status)).scalar_one() == "running"


def test_result_transaction_rolls_back_if_checkpoint_fails(engine, monkeypatch):
    task = enqueued(engine)
    claim = store.claim_task(engine, task["id"], now=NOW)
    monkeypatch.setattr(store, "_finish", Mock(side_effect=RuntimeError("disconnect")))
    with pytest.raises(RuntimeError):
        store.complete_task(
            engine,
            task["id"],
            claim["lease_token"],
            summarize_window(points(target()), target(), START, END),
            now=NOW,
        )
    with engine.connect() as connection:
        assert connection.execute(select(machines)).first() is None
        assert connection.execute(select(tasks.c.status)).scalar_one() == "running"
