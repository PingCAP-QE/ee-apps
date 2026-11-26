"""V3 durable outbox/checkpoints and small guarded transactions; no network I/O.

A task's result, daily contribution and done transition commit together. Kafka is
at-least-once transport only. There are no weekly or fixed repo/Job rollup rows.
"""

import json
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, date, timedelta

from sqlalchemy import Integer, and_, case, cast, func, literal_column, or_, select, text, tuple_
from sqlalchemy.exc import DBAPIError

from cost_insight.jobs.state_store import checkpoint_job_watermark
from cost_insight.utilization import policy
from cost_insight.utilization.aggregate import identity, minute, summarize_window, validate_stats
from cost_insight.utilization.schema import (
    MACHINE_STATS,
    TABLES,
    WORKLOAD_STATS,
    contexts,
    machines,
    tasks,
    workloads,
)

MAX_BATCH = 1000
MAX_BILL_DAYS = 8
MACHINE_FIELDS = ("region", "cluster_id", "machine_id", "resource_class")
TERMINAL = ("done", "expired")


@contextmanager
def transaction(engine):
    with engine.connect() as connection:
        if connection.dialect.name != "sqlite":
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        connection.exec_driver_sql(
            "BEGIN IMMEDIATE" if connection.dialect.name == "sqlite" else "BEGIN PESSIMISTIC"
        )
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def database_now(connection, now=None):
    if now is not None:
        if connection.dialect.name != "sqlite":
            raise ValueError("clock injection is offline SQLite only")
        return policy.utc_naive(now)
    query = (
        "SELECT CURRENT_TIMESTAMP"
        if connection.dialect.name == "sqlite"
        else "SELECT UTC_TIMESTAMP(6)"
    )
    return policy.utc_naive(connection.execute(text(query)).scalar_one())


def _state(connection, *, lock=False):
    suffix = " FOR UPDATE" if lock and connection.dialect.name != "sqlite" else ""
    value = connection.execute(
        text("SELECT watermark_json FROM cost_job_state WHERE job_name=:key" + suffix),
        {"key": policy.GUARD_KEY},
    ).scalar()
    return value if isinstance(value, dict) else json.loads(value) if value else {}


def load_state(engine):
    with engine.connect() as connection:
        return _state(connection)


def _save(connection, state):
    checkpoint_job_watermark(connection, policy.GUARD_KEY, state)


def _admit(state, clock, *, delta=0):
    health = policy.health(state, clock, projected_rows=state.get("retained_rows", 0) + delta)
    if not health["allowed"]:
        raise ValueError("utilization disabled/unhealthy: " + ",".join(health["reasons"]))


def _finish(connection, state, now=None, *, delta=0):
    clock = database_now(connection, now)
    _admit(state, clock, delta=delta)
    state["retained_rows"] += delta
    _save(connection, state)
    return clock


def configure(engine, profile, *, start_date, enabled=False, dry_run=False, now=None):
    from cost_insight.utilization.tencent import collection_scope, validate_profile

    validate_profile(profile)
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    machine_scope = collection_scope(profile)
    pod_enabled = bool(enabled and profile.get("pod_collection_enabled", False))
    contracts = {k: profile["contracts"][k] for k in ("cvm", "tke_native")}
    scope = machine_scope
    if pod_enabled:
        contracts["pod"] = profile["contracts"]["pod"]
        scope = collection_scope(profile, kind="pod")
    if len(json.dumps(contracts)) > 65536:
        raise ValueError("unbounded source contract")
    if enabled:
        for metrics in contracts.values():
            for entry in metrics.values():
                policy.validate_source_window(entry)
                if not entry.get("horizon_verified_at") or not entry.get("evidence_reference"):
                    raise ValueError("record source canary evidence before enabling")
    if dry_run:
        return {"dry_run": True, "state": load_state(engine)}
    with transaction(engine) as connection:
        state = _state(connection, lock=True)
        clock = database_now(connection, now)
        if start_date > policy.usage_date(clock):
            raise ValueError("collection start is in the future")
        if state:
            if (
                state.get("version") != policy.GUARD_VERSION
                or state["contract_version"] != profile["contract_version"]
                or any(state["contracts"].get(k) != contracts[k] for k in ("cvm", "tke_native"))
                or any(state["scope"].get(k) != value for k, value in machine_scope.items())
                or state["start_date"] != start_date.isoformat()
            ):
                raise ValueError("incompatible machine registry; never reinterpret existing history")
            was_enabled = state.get("pod_collection_enabled") is True
            if pod_enabled:
                if was_enabled:
                    if (
                        state.get("pod_contract_version") != profile["pod_contract_version"]
                        or state["contracts"].get("pod") != contracts["pod"]
                        or state["scope"] != scope
                    ):
                        raise ValueError("stop and drain Pod collection before changing its contract")
                else:
                    if _pod_work_remaining(connection):
                        raise ValueError("drain Pod tasks and contexts before reopening collection")
                    state.update(
                        contracts=contracts,
                        scope=scope,
                        pod_contract_version=profile["pod_contract_version"],
                        pod_start_at=clock.replace(tzinfo=UTC).isoformat(),
                    )
            elif was_enabled:
                # The guard stops writes and planning now; maintenance closes the
                # confirmed intervals in bounded batches using this same boundary.
                state["pod_stop_at"] = clock.replace(tzinfo=UTC).isoformat()
            state.update(enabled=bool(enabled), pod_collection_enabled=pod_enabled)
            _save(connection, state)
        else:
            state = {
                "version": policy.GUARD_VERSION,
                "enabled": bool(enabled),
                "contracts": contracts,
                "scope": scope,
                "start_date": start_date.isoformat(),
                "contract_version": profile["contract_version"],
                "pod_collection_enabled": pod_enabled,
                "pod_stop_at": None,
                "maintenance_at": None,
                "paused": True,
                "pause_reasons": ["maintenance_uninitialized"],
                "retained_rows": 0,
                "counts_reliable": False,
                "observed_bytes": None,
                "bytes_observed_at": None,
            }
            if pod_enabled:
                state.update(
                    pod_contract_version=profile["pod_contract_version"],
                    pod_start_at=clock.replace(tzinfo=UTC).isoformat(),
                )
            connection.execute(
                text(
                    "INSERT INTO cost_job_state (job_name,watermark_json,last_status,updated_at) "
                    "VALUES (:key,:state,'configured',CURRENT_TIMESTAMP)"
                ),
                {"key": policy.GUARD_KEY, "state": json.dumps(state)},
            )
        return dict(state)


def _pod_work_remaining(connection):
    return any(
        connection.execute(select(table.c.id).where(
            table.c.account_id == policy.ACCOUNT_ID, predicate
        ).limit(1)).first()
        for table, predicate in ((contexts, True), (tasks, tasks.c.kind == "pod"))
    )


def _contract_version(state, kind):
    return state["pod_contract_version" if kind == "pod" else "contract_version"]


def _entry(state, target, metric):
    return state["contracts"]["pod" if target["kind"] == "pod" else target["resource_class"]][
        metric
    ]


def deadline(state, target, start, end):
    ends = []
    for metric in policy.METRICS:
        entry = _entry(state, target, metric)
        policy.validate_source_window(entry)
        ends.append(
            start
            + timedelta(
                seconds=entry["verified_horizon_seconds"] - entry.get("safety_margin_seconds", 0)
            )
        )
    if target["kind"] == "pod":
        ends.extend((policy.expires_at(policy.usage_date(start)), end + timedelta(hours=24)))
    return min(ends)


def _machine_billing_key(region, cluster, machine):
    return identity(region, cluster, machine)


def _check_billing_generation(state, generation):
    if state.get("machine_billing_generation", 0) != generation:
        raise ValueError("billing conflicts changed during scan; rescan the complete BillDay set")


def reconcile_machine_billing_conflicts(engine, scanned, detected, *, generation, now=None, dry_run=False):
    """Only a complete rescan of every implicated BillDay can clear a conflict."""
    with transaction(engine) if not dry_run else engine.connect() as connection:
        state, clock = _state(connection, lock=not dry_run), database_now(connection, now)
        if not dry_run:
            _admit(state, clock)
        _check_billing_generation(state, generation)
        conflicts = {
            key: entry for key, entry in state.get("machine_billing_conflicts", {}).items()
            if policy.utc_naive(entry["until"]) > clock
        }
        for key, entry in list(conflicts.items()):
            if key not in detected and set(entry["bill_days"]) <= scanned:
                del conflicts[key]
        hold = state.get("machine_billing_hold_until")
        hold = hold if hold and policy.utc_naive(hold) > clock else None
        reconfirmed = False
        for key, entry in detected.items():
            if policy.utc_naive(entry["until"]) <= clock:
                continue
            # New conflicting evidence invalidates older scans even if the
            # window, known partitions and conservative hold are unchanged.
            reconfirmed = True
            old = conflicts.get(key)
            if old:
                entry = {
                    "start": min(entry["start"], old["start"]),
                    "end": max(entry["end"], old["end"]),
                    "until": max(entry["until"], old["until"]),
                    "bill_days": sorted(set(entry["bill_days"]) | set(old["bill_days"])),
                }
            if (old is None and len(conflicts) >= MAX_BATCH) or len(entry["bill_days"]) > MAX_BILL_DAYS:
                # Never forget an unpersistable conflict and admit its windows.
                hold = max(hold or entry["until"], entry["until"])
            else:
                conflicts[key] = entry
        if not dry_run and (
            reconfirmed or conflicts != state.get("machine_billing_conflicts", {})
            or hold != state.get("machine_billing_hold_until")
        ):
            generation += 1
            state.update(machine_billing_conflicts=conflicts, machine_billing_hold_until=hold,
                         machine_billing_generation=generation)
            _finish(connection, state, now)
        return conflicts, hold, generation


def _machine_bill_block(state, target, start, end, clock):
    hold = state.get("machine_billing_hold_until")
    if hold and policy.utc_naive(hold) > clock:
        return "billing conflict storage limit exceeded; wait for the affected horizon to close"
    key = _machine_billing_key(target["region"], target.get("cluster_id", ""), target["machine_id"])
    conflict = state.get("machine_billing_conflicts", {}).get(key)
    if (conflict and policy.utc_naive(conflict["until"]) > clock
            and start < policy.utc_naive(conflict["end"]) and end > policy.utc_naive(conflict["start"])):
        return "unresolved historical billing conflict; rescan BillDays together: " + ", ".join(conflict["bill_days"])
    return None


def _machine_snapshot(payload):
    # Enclosing billing bounds never change the monitor query contract or identity.
    return {k: v for k, v in payload.items() if k not in {"observed_from", "observed_until"}}


def _enqueue(connection, state, target, start, end, clock, *, context_id=None):
    start, end = policy.utc_naive(start), policy.utc_naive(end)
    if (
        start != minute(start)
        or end != minute(end)
        or not start < end
        or end - start > timedelta(minutes=30)
    ):
        raise ValueError("invalid completed task window")
    day = policy.usage_date(start)
    if day != policy.usage_date(end - timedelta(microseconds=1)):
        raise ValueError("split task at Beijing midnight")
    if day < date.fromisoformat(state["start_date"]):
        return 0
    if end > clock:
        raise ValueError("cannot collect a future window")
    if target.get("account_id", policy.ACCOUNT_ID) != policy.ACCOUNT_ID:
        raise ValueError("wrong task account")
    if (
        target["kind"] not in {"machine", "pod"}
        or target["region"] not in state["scope"]["regions"]
    ):
        raise ValueError("task outside configured source")
    kind = target["kind"]
    is_machine = kind == "machine"
    if not is_machine and (
        context_id is None
        or state.get("pod_collection_enabled") is not True
        or target["cluster_id"] != state["scope"]["cluster_id"]
        or target["namespace"] not in state["scope"]["namespaces"]
    ):
        raise ValueError("Pod tasks require an enabled, in-scope historical context")
    contract = _contract_version(state, kind)
    payload = {"contract_version": contract}
    if is_machine:
        # Billing bounds and current/historical specs are not query parameters.
        payload.update({key: target.get(key, "") for key in MACHINE_FIELDS})
        resource_class, machine_id = payload["resource_class"], payload["machine_id"]
        expected_cluster = "" if resource_class == "cvm" else state["scope"]["cluster_id"]
        if (
            resource_class not in ("cvm", "tke_native")
            or not isinstance(machine_id, str)
            or not 1 <= len(machine_id) <= 255
            or payload["cluster_id"] != expected_cluster
            or (expected_cluster and payload["region"] != state["scope"]["cluster_region"])
        ):
            raise ValueError("invalid canonical machine identity or source scope")
        if reason := _machine_bill_block(state, payload, start, end, clock):
            raise ValueError(reason)
    selector = (
        kind,
        policy.ACCOUNT_ID,
        target["region"],
        target.get("cluster_id", ""),
        target.get("machine_id") or target.get("pod_uid"),
    )
    key = identity(*selector, start.isoformat(), end.isoformat(), contract)
    previous = connection.execute(
        select(tasks.c.payload_json, tasks.c.context_id).where(tasks.c.id == key)
    ).first()
    if previous:
        snapshot = _machine_snapshot(previous.payload_json) if is_machine else previous.payload_json
        if snapshot != payload or previous.context_id != context_id:
            raise ValueError("conflicting snapshot for existing window; explicit repair required")
        return 0
    if is_machine:
        # A growing window may reuse retained checkpoints only as an exact prefix.
        cursor = start
        fragments = select(tasks.c.window_start, tasks.c.window_end, tasks.c.payload_json).where(
            tasks.c.account_id == policy.ACCOUNT_ID,
            tasks.c.kind == kind,
            tasks.c.payload_json["machine_id"].as_string() == payload["machine_id"],
            tasks.c.payload_json["region"].as_string() == payload["region"],
            tasks.c.payload_json["cluster_id"].as_string() == payload["cluster_id"],
            tasks.c.window_start < end,
            tasks.c.window_end > start,
        ).order_by(tasks.c.window_start)
        for first, stop, snapshot in connection.execute(fragments):
            if (
                first != cursor
                or not cursor < stop <= end
                or stop != minute(stop)
                or _machine_snapshot(snapshot) != payload
            ):
                raise ValueError("overlapping task windows require explicit repair, never double-add")
            cursor = stop
        if cursor == end:
            return 0
        if cursor != start:
            start = cursor
            key = identity(*selector, start.isoformat(), end.isoformat(), contract)
    limit = deadline(state, target, start, end)
    if limit <= clock and context_id is None:
        raise ValueError("source window has expired; never recreate a purged checkpoint")
    if is_machine and deadline(state, target, policy.day_start(day), end) <= clock:
        # Once the day's earliest checkpoint could have expired, a shifted
        # boundary must not re-add time already represented in the daily fact.
        raise ValueError("machine usage day has expired; never reopen purged checkpoints")
    # The Pod cursor advances atomically with each plan; different context
    # versions are already disjoint by the watcher's interval admission rules.
    due = clock if is_machine else max(clock, end + timedelta(minutes=10))
    connection.execute(
        tasks.insert().values(
            id=key,
            account_id=policy.ACCOUNT_ID,
            kind=target["kind"],
            context_id=context_id,
            payload_json=payload,
            window_start=start,
            window_end=end,
            due_at=due,
            deadline_at=limit,
            status="pending",
        )
    )
    return 1


def enqueue_windows(engine, windows, *, now=None, dry_run=False, billing_generation=None):
    if len(windows) > MAX_BATCH:
        raise ValueError("bounded task batch required")
    if dry_run:
        return {"dry_run": True, "planned": len(windows)}
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        _admit(state, clock)
        if billing_generation is not None:
            _check_billing_generation(state, billing_generation)
        added = sum(
            _enqueue(connection, state, target, start, end, clock) for target, start, end in windows
        )
        _finish(connection, state, now, delta=added)
        return {"planned": added}


def _pod_target(row, state):
    target = {
        key: row[key]
        for key in (
            "account_id",
            "cluster_id",
            "namespace",
            "pod_uid",
            "pod_name",
            "node_instance_id",
            "cpu_request_cores",
            "memory_request_bytes",
            "labels_json",
        )
    }
    return {**target, "kind": "pod", "region": state["scope"]["cluster_region"]}


def _task_target(connection, task, state):
    if task["kind"] == "machine":
        target = {k: task[k] for k in ("kind", "account_id")}
        target.update({k: task["payload_json"][k] for k in MACHINE_FIELDS})
    else:
        row = connection.execute(select(contexts).where(
            contexts.c.id == task["context_id"], contexts.c.account_id == task["account_id"]
        )).mappings().one()
        target = _pod_target(row, state)
    return {**target, "contract_version": task["payload_json"]["contract_version"]}


def _context_ready_to_plan(connection, clock):
    """Require a whole due minute for a tail, or a due 10-minute boundary if open."""
    observed = contexts.c.observed_until
    closed = contexts.c.closed_at.is_not(None)
    cutoff = minute(clock - timedelta(minutes=10))
    last_due_boundary = cutoff.replace(minute=cutoff.minute // 10 * 10)
    # A closed context can stop before its next boundary, but only if its
    # confirmed end is itself due. Do not create a synthetic cutoff window.
    due = or_(
        contexts.c.planned_until <= last_due_boundary - timedelta(minutes=1),
        and_(closed, observed < cutoff + timedelta(minutes=1)),
    )
    # Truncate before arithmetic: SQLite can round fractional seconds forward.
    if connection.dialect.name == "sqlite":
        observed_minute = cast(func.substr(observed, 15, 2), Integer)
        back = case((closed, 1), else_=observed_minute % 10 + 1)
        confirmed = (
            func.datetime(func.substr(observed, 1, 16) + ":00", func.printf("-%d minutes", back))
            + ".000000"
        )
    else:
        back = case((closed, 1), else_=func.minute(observed) % 10 + 1)
        confirmed = func.timestampadd(
            literal_column("MINUTE"), -back, func.date_format(observed, "%Y-%m-%d %H:%i:00")
        )
    return and_(due, contexts.c.planned_until <= confirmed)


def plan_contexts(engine, *, now=None, limit=1000):
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError("invalid context planning limit")
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        _admit(state, clock)
        if state.get("pod_collection_enabled") is not True:
            return {"planned": 0}
        # Limit new Pod tasks by real headroom; an empty planning batch must
        # still let the dispatcher publish existing tasks.
        limit = min(limit, policy.ROW_LIMIT - 1 - state["retained_rows"])
        if limit <= 0:
            return {"planned": 0}
        rows = (
            connection.execute(
                select(contexts)
                .where(
                    contexts.c.account_id == policy.ACCOUNT_ID,
                    contexts.c.expires_at > clock,
                    _context_ready_to_plan(connection, clock),
                )
                .order_by(contexts.c.planned_until, contexts.c.id)
                .limit(limit)
            )
            .mappings()
            .all()
        )
        added = 0
        for row in rows:
            cursor, end = minute(row["planned_until"], ceil=True), minute(row["observed_until"])
            if cursor >= end:
                continue
            target = _pod_target(row, state)
            while cursor < end and added < limit:
                boundary = cursor.replace(
                    minute=cursor.minute // 10 * 10, second=0, microsecond=0
                ) + timedelta(minutes=10)
                stop = min(boundary, end)
                if not row["closed_at"] and boundary > end:
                    break
                if stop + timedelta(minutes=10) > clock:
                    break
                added += _enqueue(
                    connection, state, target, cursor, stop, clock, context_id=row["id"]
                )
                cursor = stop
            connection.execute(
                contexts.update().where(contexts.c.id == row["id"]).values(planned_until=cursor)
            )
        _finish(connection, state, now, delta=added)
        return {"planned": added}


def due_tasks(engine, *, now=None, limit=100):
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError("invalid dispatch limit")
    with engine.connect() as connection:
        clock = database_now(connection, now)
        _admit(_state(connection), clock)
        return [
            dict(row)
            for row in connection.execute(
                select(tasks)
                .where(
                    tasks.c.account_id == policy.ACCOUNT_ID,
                    tasks.c.due_at <= clock,
                    tasks.c.deadline_at > clock,
                    or_(
                        tasks.c.status.in_(("pending", "retry")),
                        and_(tasks.c.status == "running", tasks.c.lease_until <= clock),
                    ),
                    or_(
                        tasks.c.dispatched_at.is_(None),
                        tasks.c.dispatched_at <= clock - timedelta(minutes=5),
                    ),
                )
                .order_by(tasks.c.due_at, tasks.c.id)
                .limit(limit)
            ).mappings()
        ]


def mark_dispatched(engine, task_id, *, now=None):
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        _admit(state, clock)
        connection.execute(
            tasks.update()
            .where(
                tasks.c.id == task_id,
                tasks.c.account_id == policy.ACCOUNT_ID,
                tasks.c.status.in_(("pending", "retry", "running")),
            )
            .values(dispatched_at=clock)
        )
        _finish(connection, state, now)


def claim_task(engine, task_id, *, now=None):
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        _admit(state, clock)
        row = (
            connection.execute(
                select(tasks).where(tasks.c.id == task_id, tasks.c.account_id == policy.ACCOUNT_ID)
            )
            .mappings()
            .first()
        )
        if (
            not row
            or row["status"] in TERMINAL
            or row["due_at"] > clock
            or row["deadline_at"] <= clock
            or (row["status"] == "running" and row["lease_until"] > clock)
        ):
            return None
        target = _task_target(connection, row, state)
        values = {
            "status": "running",
            "lease_token": uuid.uuid4().hex,
            "lease_until": min(row["deadline_at"], clock + timedelta(minutes=5)),
        }
        connection.execute(tasks.update().where(tasks.c.id == task_id).values(**values))
        _finish(connection, state, now)
        return {**dict(row), **values, "payload_json": target}


def _apply(connection, task, values, clock):
    target = task["payload_json"]
    machine = task["kind"] == "machine"
    validate_stats(values, machine=machine)
    day = policy.usage_date(task["window_start"])
    minutes = (task["window_end"] - task["window_start"]) // timedelta(minutes=1)
    keys = {"account_id": target["account_id"], "usage_date": day}
    if machine:
        samples = [values[f"{metric}_sample_minutes"] for metric in policy.METRICS]
        if max(samples) > minutes:
            raise ValueError("machine samples exceed task window")
        if not any(samples):
            return 0
        table, fields = machines, MACHINE_STATS
        keys.update({k: target[k] for k in ("region", "cluster_id", "machine_id")})
        extra = {"resource_class": target["resource_class"]}
    else:
        if day < policy.workload_start_date(clock):
            raise ValueError("workload day is expired")
        if values["expected_pod_minutes"] != minutes:
            raise ValueError("Pod expected minutes differ from task window")
        table, fields = workloads, WORKLOAD_STATS
        labels = target["labels_json"]
        allowed = _state(connection)["scope"]["labels"]
        if (
            not isinstance(labels, dict)
            or set(labels) - set(allowed)
            or len(json.dumps(labels).encode()) > 8192
            or any(not isinstance(value, str) or not value or len(value) > 512 for value in labels.values())
        ):
            raise ValueError("unbounded/unapproved labels")
        keys["group_key"] = identity(labels)
        extra = {"labels_json": labels}
    predicate = and_(*(table.c[k] == v for k, v in keys.items()))
    previous = connection.execute(select(table).where(predicate)).mappings().first()
    stats = {field: values[field] for field in fields}
    if previous:
        if any(previous[k] != value for k, value in extra.items()):
            raise ValueError("conflicting daily fact snapshot")
        for field, value in stats.items():
            if field.endswith("_peak_percent"):
                stats[field] = max((v for v in (previous[field], value) if v is not None), default=None)
            else:
                stats[field] = previous[field] + value
        connection.execute(table.update().where(predicate).values(**stats))
        return 0
    connection.execute(table.insert().values(**keys, **extra, **stats))
    return 1


def complete_task(engine, task_id, token, values, *, now=None):
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        _admit(state, clock)
        row = (
            connection.execute(
                select(tasks).where(tasks.c.id == task_id, tasks.c.account_id == policy.ACCOUNT_ID)
            )
            .mappings()
            .first()
        )
        if (
            not row
            or row["status"] != "running"
            or row["lease_token"] != token
            or clock >= row["lease_until"]
            or clock >= row["deadline_at"]
        ):
            return False
        if row["payload_json"].get("contract_version") != _contract_version(state, row["kind"]):
            raise ValueError("task contract differs from registry")
        target = _task_target(connection, row, state)
        delta = _apply(connection, {**row, "payload_json": target}, values, clock)
        connection.execute(
            tasks.update()
            .where(tasks.c.id == task_id)
            .values(status="done", lease_token=None, lease_until=None)
        )
        _finish(connection, state, now, delta=delta)
        # Last decision under the guard, after all writes. If either fence has
        # passed, rollback both the contribution and its completion checkpoint.
        if database_now(connection, now) >= min(row["lease_until"], row["deadline_at"]):
            raise ValueError("task lease/deadline passed during result commit")
        return True


def fail_task(engine, task_id, token, *, now=None):
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        _admit(state, clock)
        row = (
            connection.execute(
                select(tasks).where(tasks.c.id == task_id, tasks.c.account_id == policy.ACCOUNT_ID)
            )
            .mappings()
            .first()
        )
        if (
            not row
            or row["status"] != "running"
            or row["lease_token"] != token
            or clock >= row["lease_until"]
        ):
            return False
        # Only maintenance settles expired tasks; retries never change the deadline.
        connection.execute(
            tasks.update()
            .where(tasks.c.id == task_id)
            .values(
                status="retry",
                lease_token=None,
                lease_until=None,
                dispatched_at=None,
                due_at=min(row["deadline_at"], clock + policy.RETRY_INTERVAL),
            )
        )
        _finish(connection, state, now)
        return True


def _bytes(connection):
    names = [t.name for t in TABLES]
    if connection.dialect.name == "sqlite":
        total = 0
        for name in names:
            total += connection.execute(
                text(
                    "SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name IN "
                    "(SELECT name FROM sqlite_master WHERE tbl_name=:name)"
                ),
                {"name": name},
            ).scalar_one()
        return int(total)
    placeholders = ",".join(f":t{i}" for i in range(len(names)))
    row = (
        connection.execute(
            text(
                "SELECT COUNT(*) AS n, COUNT(DATA_LENGTH+INDEX_LENGTH) AS measured, "
                "SUM(DATA_LENGTH+INDEX_LENGTH) AS size FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() "
                f"AND TABLE_NAME IN ({placeholders})"
            ),
            {f"t{i}": name for i, name in enumerate(names)},
        )
        .mappings()
        .one()
    )
    if row["n"] != len(names) or row["measured"] != len(names):
        raise ValueError("complete table and index byte statistics required")
    return int(row["size"])


def maintain(engine, *, now=None, dry_run=False, batch_size=1000, max_rows=50000, max_seconds=300):
    if not 1 <= batch_size <= 1000 or not 1 <= max_rows <= 50000 or not 0 < max_seconds <= 300:
        raise ValueError("invalid maintenance budget")
    if dry_run:
        return {"dry_run": True, "state": load_state(engine)}
    started, deleted, context_cursor = time.monotonic(), 0, 0
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        if state.get("version") != policy.GUARD_VERSION:
            raise ValueError("configure V3 before maintenance")
    # A successful ended Pod's work is removed immediately, not retained for weeks.
    while deleted < max_rows and time.monotonic() - started < max_seconds:
        with transaction(engine) as connection:
            state, clock = _state(connection, lock=True), database_now(connection, now)
            stop_at = state.get("pod_stop_at") if not state.get("pod_collection_enabled") else None
            stopped = policy.utc_naive(stop_at) if stop_at is not None else None
            retained_from = policy.workload_start_date(clock)
            rows = (
                connection.execute(
                    select(contexts)
                    .where(
                        contexts.c.account_id == policy.ACCOUNT_ID,
                        or_(
                            stopped is not None,
                            contexts.c.closed_at.is_not(None),
                            contexts.c.observed_until <= clock - timedelta(minutes=15),
                        ),
                        contexts.c.id > context_cursor,
                    )
                    .order_by(contexts.c.id)
                    .limit(batch_size)
                )
                .mappings()
                .all()
            )
            if rows:
                context_cursor = rows[-1]["id"]
            # Close interrupted watcher intervals in the same bounded keyset batches.
            orphan_ids = [row["id"] for row in rows if row["closed_at"] is None]
            if orphan_ids:
                confirmed = contexts.c.observed_until
                if stopped is not None:
                    confirmed = case((confirmed > stopped, stopped), else_=confirmed)
                connection.execute(
                    contexts.update().where(contexts.c.id.in_(orphan_ids))
                    .values(closed_at=confirmed, observed_until=confirmed)
                )
            removable, finalized = [], 0
            for row in rows:
                planned_until = row["planned_until"]
                end = minute(min(row["observed_until"], stopped) if stopped else row["observed_until"])
                if (stopped is not None or row["expires_at"] <= clock) and finalized < batch_size:
                    # A stopped or expired context cannot return to the planner.
                    # Settle only its unplanned tail even while admission is paused.
                    cursor = minute(planned_until, ceil=True)
                    target = _pod_target(row, state)
                    while cursor < end and finalized < batch_size:
                        boundary = cursor.replace(minute=cursor.minute // 10 * 10) + timedelta(
                            minutes=10
                        )
                        stop = min(boundary, end)
                        day = policy.usage_date(cursor)
                        if (
                            day >= date.fromisoformat(state["start_date"])
                            and day >= retained_from
                        ):
                            missing = summarize_window([], target, cursor, stop)
                            state["retained_rows"] += _apply(
                                connection,
                                {
                                    "kind": "pod",
                                    "payload_json": target,
                                    "window_start": cursor,
                                    "window_end": stop,
                                },
                                missing,
                                clock,
                            )
                        cursor = stop
                        finalized += 1
                    planned_until = cursor
                    connection.execute(
                        contexts.update()
                        .where(contexts.c.id == row["id"])
                        .values(planned_until=planned_until)
                    )
                has_pending = connection.execute(
                    select(tasks.c.id)
                    .where(
                        tasks.c.context_id == row["id"], tasks.c.status.notin_(TERMINAL)
                    )
                    .limit(1)
                ).first()
                if not has_pending and minute(planned_until, ceil=True) >= end:
                    removable.append(row["id"])
            # A failed Pod window still contributes its confirmed population.
            expiring = (
                connection.execute(
                    select(tasks)
                    .where(
                        tasks.c.account_id == policy.ACCOUNT_ID,
                        tasks.c.deadline_at <= clock,
                        tasks.c.status.notin_(TERMINAL),
                    )
                    .order_by(tasks.c.deadline_at)
                    .limit(batch_size)
                )
                .mappings()
                .all()
            )
            for task in expiring:
                if (
                    task["kind"] == "pod"
                    and policy.usage_date(task["window_start"]) >= retained_from
                ):
                    target = _task_target(connection, task, state)
                    missing = summarize_window([], target, task["window_start"], task["window_end"])
                    state["retained_rows"] += _apply(
                        connection, {**task, "payload_json": target}, missing, clock
                    )
                connection.execute(
                    tasks.update()
                    .where(tasks.c.id == task["id"])
                    .values(status="expired", lease_token=None, lease_until=None)
                )
            # Generic expiry sweep; machine facts are deliberately not here.
            targets = [
                (
                    tasks,
                    and_(tasks.c.deadline_at <= clock, tasks.c.status.in_(TERMINAL)),
                ),
                (workloads, workloads.c.usage_date < retained_from),
            ]
            changed = 0
            for table, predicate in targets:
                budget = min(batch_size, max_rows - deleted - changed)
                if budget <= 0:
                    break
                keys = tuple(table.primary_key.columns)
                selected = connection.execute(
                    select(*keys).where(table.c.account_id == policy.ACCOUNT_ID, predicate)
                    .order_by(*keys).limit(budget)
                ).all()
                if selected:
                    match = (keys[0].in_([row[0] for row in selected]) if len(keys) == 1
                             else tuple_(*keys).in_(selected))
                    changed += connection.execute(
                        table.delete().where(match, table.c.account_id == policy.ACCOUNT_ID)
                    ).rowcount
            for ctx_id in removable:
                budget = min(batch_size, max_rows - deleted - changed)
                if budget <= 0:
                    break
                ids = (
                    connection.execute(
                        select(tasks.c.id)
                        .where(
                            tasks.c.account_id == policy.ACCOUNT_ID,
                            tasks.c.context_id == ctx_id,
                            tasks.c.status.in_(TERMINAL),
                        )
                        .limit(budget)
                    )
                    .scalars()
                    .all()
                )
                if ids:
                    changed += connection.execute(
                        tasks.delete().where(tasks.c.id.in_(ids))
                    ).rowcount
                if (
                    changed < max_rows - deleted
                    and not connection.execute(
                        select(tasks.c.id).where(tasks.c.context_id == ctx_id).limit(1)
                    ).first()
                ):
                    changed += connection.execute(
                        contexts.delete().where(
                            contexts.c.id == ctx_id, contexts.c.account_id == policy.ACCOUNT_ID
                        )
                    ).rowcount
            state["retained_rows"] = max(0, state.get("retained_rows", 0) - changed)
            _save(connection, state)
        deleted += changed
        if not changed and not rows:
            break
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        count = sum(
            connection.execute(
                select(func.count()).select_from(
                    select(table.c.account_id)
                    .where(table.c.account_id == policy.ACCOUNT_ID)
                    .limit(policy.ROW_LIMIT + 1)
                    .subquery()
                )
            ).scalar_one()
            for table in TABLES
        )
        try:
            size = _bytes(connection)
        except ValueError:  # Known incomplete measurements must invalidate old healthy evidence.
            size = None
        except DBAPIError as error:
            if error.connection_invalidated:
                raise  # A lost DB connection cannot persist guard state in this transaction.
            size = None
        overdue = []
        for expiry in (contexts.c.expires_at, tasks.c.deadline_at):
            earliest = connection.execute(
                select(func.min(expiry)).where(
                    expiry.table.c.account_id == policy.ACCOUNT_ID, expiry <= clock
                )
            ).scalar()
            if earliest is not None:
                overdue.append(earliest)
        oldest_workload = connection.execute(select(func.min(workloads.c.usage_date)).where(
            workloads.c.account_id == policy.ACCOUNT_ID,
            workloads.c.usage_date < policy.workload_start_date(clock),
        )).scalar()
        if oldest_workload is not None:
            overdue.append(policy.expires_at(oldest_workload))
        oldest = min(overdue, default=None)
        if (
            not state.get("pod_collection_enabled") and "pod" in state["contracts"]
            and not _pod_work_remaining(connection)
        ):
            del state["contracts"]["pod"]
            state.pop("pod_contract_version", None)
        recovering = state.get("paused") and "maintenance_uninitialized" not in state.get(
            "pause_reasons", []
        )
        reasons = []
        if count >= (policy.ROW_WARNING if recovering else policy.ROW_LIMIT):
            reasons.append("row_budget")
        if size is None:
            reasons.append("bytes_unreliable")
        elif size >= (policy.BYTE_WARNING if recovering else policy.BYTE_LIMIT):
            reasons.append("byte_budget")
        if oldest is not None and (recovering or clock - oldest > timedelta(hours=4)):
            reasons.append("cleanup_backlog")
        hard = bool(reasons)
        state.update(
            retained_rows=count,
            counts_reliable=True,
            observed_bytes=size,
            bytes_observed_at=clock.isoformat() if size is not None else None,
            maintenance_at=clock.isoformat(),
            paused=hard,
            pause_reasons=reasons,
        )
        _save(connection, state)
        return {
            "deleted": deleted,
            "retained_rows": count,
            "paused": hard,
            "warnings": policy.health(state, clock)["warnings"],
        }
