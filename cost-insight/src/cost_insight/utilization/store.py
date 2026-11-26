"""V2 durable outbox/checkpoints and small guarded transactions; no network I/O.

A task's result, daily contribution and done transition commit together. Kafka is
at-least-once transport only. There are no weekly or fixed repo/Job rollup rows.
"""

import json
import time
import uuid
from contextlib import contextmanager
from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select, text

from cost_insight.jobs.state_store import checkpoint_job_watermark
from cost_insight.utilization import policy
from cost_insight.utilization.aggregate import identity, minute, summarize_window, validate_stats
from cost_insight.utilization.schema import (
    STAT_FIELDS,
    TABLES,
    contexts,
    machines,
    tasks,
    workloads,
)

MAX_BATCH = 1000


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
    from cost_insight.utilization.tencent import validate_profile

    validate_profile(profile)
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    scope = {
        k: profile[k]
        for k in ("account_id", "regions", "cluster_id", "cluster_region", "namespaces")
    }
    scope["labels"] = profile.get("labels", sorted(policy.LABEL_KEYS))
    policy.label_keys(scope["labels"])
    contracts = profile["contracts"]
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
                state.get("version") != 2
                or state["contracts"] != contracts
                or state["scope"] != scope
                or state["start_date"] != start_date.isoformat()
            ):
                raise ValueError("incompatible registry; never reinterpret existing history")
            state["enabled"] = bool(enabled)
            _save(connection, state)
        else:
            state = {
                "version": 2,
                "enabled": bool(enabled),
                "contracts": contracts,
                "scope": scope,
                "start_date": start_date.isoformat(),
                "contract_version": profile["contract_version"],
                "maintenance_at": None,
                "paused": True,
                "pause_reasons": ["maintenance_uninitialized"],
                "retained_rows": 0,
                "counts_reliable": False,
                "observed_bytes": None,
                "bytes_observed_at": None,
            }
            connection.execute(
                text(
                    "INSERT INTO cost_job_state (job_name,watermark_json,last_status,updated_at) "
                    "VALUES (:key,:state,'configured',CURRENT_TIMESTAMP)"
                ),
                {"key": policy.GUARD_KEY, "state": json.dumps(state)},
            )
        return dict(state)


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


def _enqueue(connection, state, target, start, end, clock, *, context_id=None):
    start, end = policy.utc_naive(start), policy.utc_naive(end)
    if (
        start != minute(start)
        or end != minute(end)
        or not start < end
        or end - start > timedelta(minutes=30)
    ):
        raise ValueError("invalid completed task window")
    if policy.usage_date(start) != policy.usage_date(end - timedelta(microseconds=1)):
        raise ValueError("split task at Beijing midnight")
    if policy.usage_date(start) < date.fromisoformat(state["start_date"]):
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
    if target["kind"] == "pod" and (
        target["cluster_id"] != state["scope"]["cluster_id"]
        or target["namespace"] not in state["scope"]["namespaces"]
    ):
        raise ValueError("Pod outside collection scope")
    payload = {**target, "contract_version": state["contract_version"]}
    payload = json.loads(json.dumps(payload, default=str, allow_nan=False))
    if len(json.dumps(payload)) > 16384:
        raise ValueError("unbounded task payload")
    key = identity(
        target["kind"],
        policy.ACCOUNT_ID,
        target["region"],
        target.get("cluster_id", ""),
        target.get("machine_id") or target.get("pod_uid"),
        start.isoformat(),
        end.isoformat(),
        state["contract_version"],
    )
    previous = connection.execute(select(tasks).where(tasks.c.id == key)).mappings().first()
    if previous:
        if previous["payload_json"] != payload:
            raise ValueError("conflicting snapshot for existing window; explicit repair required")
        return 0
    limit = deadline(state, target, start, end)
    if limit <= clock and context_id is None:
        raise ValueError("source window has expired; never recreate a purged checkpoint")
    # An active context's unplanned exhausted tail is registered once while its
    # planned_until advances atomically; maintenance records expected missing use.
    identity_field = "machine_id" if target["kind"] == "machine" else "pod_uid"
    overlap = connection.execute(
        select(tasks.c.id)
        .where(
            tasks.c.account_id == policy.ACCOUNT_ID,
            tasks.c.kind == target["kind"],
            tasks.c.payload_json[identity_field].as_string() == target[identity_field],
            tasks.c.payload_json["region"].as_string() == target["region"],
            tasks.c.payload_json["cluster_id"].as_string() == target.get("cluster_id", ""),
            tasks.c.window_start < end,
            tasks.c.window_end > start,
        )
        .limit(1)
    ).first()
    if overlap:
        raise ValueError("overlapping task windows require explicit repair, never double-add")
    due = clock if target["kind"] == "machine" else max(clock, end + timedelta(minutes=10))
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
            attempts=0,
            updated_at=clock,
        )
    )
    return 1


def enqueue_windows(engine, windows, *, now=None, dry_run=False):
    if len(windows) > MAX_BATCH:
        raise ValueError("bounded task batch required")
    if dry_run:
        return {"dry_run": True, "planned": len(windows)}
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        _admit(state, clock, delta=len(windows))
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
            "node_query_kind",
            "node_query_value",
            "cpu_request_cores",
            "memory_request_bytes",
            "labels_json",
        )
    }
    return {**target, "kind": "pod", "region": state["scope"]["cluster_region"]}


def plan_contexts(engine, *, now=None, limit=1000):
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError("invalid context planning limit")
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        _admit(state, clock, delta=limit)
        rows = (
            connection.execute(
                select(contexts)
                .where(
                    contexts.c.account_id == policy.ACCOUNT_ID,
                    contexts.c.expires_at > clock,
                    contexts.c.planned_until < contexts.c.observed_until,
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
            or row["status"] in {"done", "expired"}
            or row["due_at"] > clock
            or row["deadline_at"] <= clock
            or (row["status"] == "running" and row["lease_until"] > clock)
        ):
            return None
        token = uuid.uuid4().hex
        values = {
            "status": "running",
            "attempts": row["attempts"] + 1,
            "lease_token": token,
            "lease_until": min(row["deadline_at"], clock + timedelta(minutes=5)),
            "updated_at": clock,
        }
        connection.execute(tasks.update().where(tasks.c.id == task_id).values(**values))
        _finish(connection, state, now)
        return {**dict(row), **values}


def _apply(connection, task, values, clock):
    target = task["payload_json"]
    machine = task["kind"] == "machine"
    validate_stats(values, machine=machine)
    day = policy.usage_date(task["window_start"])
    if machine:
        table = machines
        identity_values = {
            "account_id": policy.ACCOUNT_ID,
            "region": target["region"],
            "cluster_id": target.get("cluster_id", ""),
            "machine_id": target["machine_id"],
            "usage_date": day,
        }
        extra = {k: target.get(k) for k in ("cpu_cores", "memory_bytes")}
        extra.update(
            resource_class=target["resource_class"], instance_type=target.get("instance_type", "")
        )
    else:
        table = workloads
        labels = target["labels_json"]
        allowed = _state(connection)["scope"]["labels"]
        if (
            not isinstance(labels, dict)
            or set(labels) - set(allowed)
            or len(json.dumps(labels)) > 8192
            or any(not isinstance(value, str) or len(value) > 512 for value in labels.values())
        ):
            raise ValueError("unbounded/unapproved labels")
        key = identity(labels, target.get("cpu_request_cores"), target.get("memory_request_bytes"))
        identity_values = {"account_id": policy.ACCOUNT_ID, "usage_date": day, "group_key": key}
        extra = {
            "labels_json": labels,
            "cpu_request_cores": target.get("cpu_request_cores"),
            "memory_request_bytes": target.get("memory_request_bytes"),
            "expires_at": policy.expires_at(day),
        }
        if clock >= extra["expires_at"]:
            raise ValueError("workload day is expired")
    predicate = and_(*(table.c[k] == v for k, v in identity_values.items()))
    previous = connection.execute(select(table).where(predicate)).mappings().first()
    row = {
        **identity_values,
        **extra,
        "updated_at": clock,
        "cutoff": max(task["window_end"], previous["cutoff"] if previous else task["window_end"]),
        "quality_json": sorted(
            set(values.get("quality_json", [])) | set(previous["quality_json"] if previous else [])
        ),
    }
    for metric in policy.METRICS:
        for field in STAT_FIELDS:
            column = f"{metric}_{field}"
            row[column] = values[column] + (previous[column] if previous else 0)
        if machine:
            name = f"{metric}_peak"
            use_old = (
                previous
                and previous[name] is not None
                and (values[name] is None or previous[name] >= values[name])
            )
            row[name] = previous[name] if use_old else values[name]
            row[name + "_at"] = previous[name + "_at"] if use_old else values[name + "_at"]
    if machine and previous:
        for key in ("cpu_cores", "memory_bytes"):
            if previous[key] != row[key]:
                row[key] = None  # Integrals remain correct for a resized machine.
        if previous["instance_type"] != row["instance_type"]:
            row["instance_type"] = "mixed"
    if previous:
        connection.execute(table.update().where(table.c.id == previous["id"]).values(**row))
        return 0
    connection.execute(table.insert().values(**row))
    return 1


def complete_task(engine, task_id, token, values, *, now=None):
    with transaction(engine) as connection:
        state, clock = _state(connection, lock=True), database_now(connection, now)
        _admit(state, clock, delta=1)
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
        if row["payload_json"].get("contract_version") != state["contract_version"]:
            raise ValueError("task contract differs from registry")
        delta = _apply(connection, row, values, clock)
        # Recheck authoritative clock after result work and before commit.
        commit_clock = database_now(connection, now)
        if commit_clock >= min(row["lease_until"], row["deadline_at"]):
            raise ValueError("task lease/deadline passed during result commit")
        connection.execute(
            tasks.update()
            .where(tasks.c.id == task_id)
            .values(status="done", lease_token=None, lease_until=None, updated_at=clock)
        )
        _finish(connection, state, now, delta=delta)
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
        # Only maintenance expires tasks at their deadline; attempts never shorten it.
        connection.execute(
            tasks.update()
            .where(tasks.c.id == task_id)
            .values(
                status="retry",
                lease_token=None,
                lease_until=None,
                dispatched_at=None,
                due_at=clock + timedelta(seconds=min(300, 2 ** min(row["attempts"], 9))),
                updated_at=clock,
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
        if state.get("version") != 2:
            raise ValueError("configure V2 before maintenance")
    # A successful ended Pod's work is removed immediately, not retained for weeks.
    while deleted < max_rows and time.monotonic() - started < max_seconds:
        with transaction(engine) as connection:
            state, clock = _state(connection, lock=True), database_now(connection, now)
            rows = (
                connection.execute(
                    select(contexts)
                    .where(
                        contexts.c.account_id == policy.ACCOUNT_ID,
                        or_(
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
                connection.execute(
                    contexts.update()
                    .where(contexts.c.id.in_(orphan_ids))
                    .values(closed_at=contexts.c.observed_until)
                )
            removable, finalized = [], 0
            for row in rows:
                planned_until = row["planned_until"]
                if row["expires_at"] <= clock and finalized < batch_size:
                    # Expired contexts cannot return to the dispatcher. Finalize only
                    # their unplanned whole-minute tail, even while admission is paused.
                    cursor = minute(planned_until, ceil=True)
                    end = minute(row["observed_until"])
                    target = _pod_target(row, state)
                    while cursor < end and finalized < batch_size:
                        boundary = cursor.replace(minute=cursor.minute // 10 * 10) + timedelta(
                            minutes=10
                        )
                        stop = min(boundary, end)
                        day = policy.usage_date(cursor)
                        if (
                            day >= date.fromisoformat(state["start_date"])
                            and policy.expires_at(day) > clock
                        ):
                            missing = summarize_window([], target, cursor, stop)
                            missing["quality_json"].append("collection_failed")
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
                        tasks.c.context_id == row["id"], tasks.c.status.notin_(("done", "expired"))
                    )
                    .limit(1)
                ).first()
                if not has_pending and planned_until >= minute(row["observed_until"]):
                    removable.append(row["id"])
            # A failed window still contributes its known expected population.
            expiring = (
                connection.execute(
                    select(tasks)
                    .where(
                        tasks.c.account_id == policy.ACCOUNT_ID,
                        tasks.c.deadline_at <= clock,
                        tasks.c.status.notin_(("done", "expired")),
                    )
                    .order_by(tasks.c.deadline_at)
                    .limit(batch_size)
                )
                .mappings()
                .all()
            )
            for task in expiring:
                if (
                    task["kind"] == "machine"
                    or policy.expires_at(policy.usage_date(task["window_start"])) > clock
                ):
                    missing = summarize_window(
                        [], task["payload_json"], task["window_start"], task["window_end"]
                    )
                    missing["quality_json"].append("collection_failed")
                    state["retained_rows"] += _apply(connection, task, missing, clock)
                connection.execute(
                    tasks.update()
                    .where(tasks.c.id == task["id"])
                    .values(status="expired", updated_at=clock)
                )
            # Generic expiry sweep; machine facts are deliberately not here.
            targets = [
                (
                    tasks,
                    and_(tasks.c.deadline_at <= clock, tasks.c.status.in_(("done", "expired"))),
                ),
                (workloads, workloads.c.expires_at <= clock),
            ]
            changed = 0
            for table, predicate in targets:
                budget = min(batch_size, max_rows - deleted - changed)
                if budget <= 0:
                    break
                ids = (
                    connection.execute(
                        select(table.c.id)
                        .where(table.c.account_id == policy.ACCOUNT_ID, predicate)
                        .order_by(table.c.id)
                        .limit(budget)
                    )
                    .scalars()
                    .all()
                )
                if ids:
                    changed += connection.execute(
                        table.delete().where(
                            table.c.id.in_(ids), table.c.account_id == policy.ACCOUNT_ID
                        )
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
                            tasks.c.status.in_(("done", "expired")),
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
                    select(table.c.id)
                    .where(table.c.account_id == policy.ACCOUNT_ID)
                    .limit(policy.ROW_LIMIT + 1)
                    .subquery()
                )
            ).scalar_one()
            for table in TABLES
        )
        size = _bytes(connection)
        oldest = connection.execute(
            select(func.min(contexts.c.expires_at)).where(
                contexts.c.account_id == policy.ACCOUNT_ID, contexts.c.expires_at <= clock
            )
        ).scalar()
        recovering = state.get("paused") and "maintenance_uninitialized" not in state.get(
            "pause_reasons", []
        )
        reasons = []
        if count >= (policy.ROW_WARNING if recovering else policy.ROW_LIMIT):
            reasons.append("row_budget")
        if size >= (policy.BYTE_WARNING if recovering else policy.BYTE_LIMIT):
            reasons.append("byte_budget")
        if oldest is not None and (recovering or clock - oldest > timedelta(hours=4)):
            reasons.append("cleanup_backlog")
        hard = bool(reasons)
        state.update(
            retained_rows=count,
            counts_reliable=True,
            observed_bytes=size,
            bytes_observed_at=clock.isoformat(),
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
