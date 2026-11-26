"""Tencent-only planners and Kafka workers. Timers live in DB, never per-Pod threads."""

import json
import logging
from datetime import date, timedelta

from cost_insight.utilization import policy, store
from cost_insight.utilization.aggregate import minute, summarize_window
from cost_insight.utilization.tencent import TencentReader

LOG = logging.getLogger(__name__)


def _ready(engine, profile, now=None):
    state = store.load_state(engine)
    if state.get("contracts") != profile["contracts"]:
        raise ValueError("configure the exact V2 profile before collection")
    with engine.connect() as connection:
        clock = store.database_now(connection, now)
    return state, clock


def _first_open_machine_day(state, kind, clock, oldest):
    horizons = []
    retained_seconds = (clock - policy.day_start(oldest)).total_seconds()
    for entry in state["contracts"][kind].values():
        policy.validate_source_window(entry)
        horizons.append(entry["verified_horizon_seconds"] - entry.get("safety_margin_seconds", 0))
    horizon = min(horizons)
    if horizon > retained_seconds:
        return policy.day_start(oldest)
    # Match the store fence: once any early checkpoint could have expired,
    # no new windows can be planned for that whole usage day.
    last_closed_day = policy.usage_date(clock - timedelta(seconds=horizon))
    return policy.day_start(last_closed_day + timedelta(days=1))


def plan_machines(engine, profile, *, bill_day=None, bill_days=None, dry_run=False, now=None, reader=None):
    state, clock = _ready(engine, profile, now)
    if not dry_run:
        store._admit(state, clock)
    today = policy.usage_date(clock)
    oldest = today - timedelta(days=364)
    if bill_day is not None and bill_days is not None:
        raise ValueError("supply bill_day or bill_days, not both")
    partitions = sorted(set(bill_days if bill_days is not None else (
        [bill_day] if bill_day else [today - timedelta(days=n) for n in (4, 3)]
    )))
    if not 1 <= len(partitions) <= 8 or any(
        not date(1970, 1, 1) <= part <= today for part in partitions
    ):
        raise ValueError("scan 1..8 distinct BillDays between 1970-01-01 and today")
    first_open = {
        kind: _first_open_machine_day(state, kind, clock, oldest)
        for kind in ("cvm", "tke_native")
    }
    reader = reader or TencentReader(profile)
    reader.verify_account()
    # No DB writes until all requested partitions and both products have completed.
    by_machine = {}
    for partition in partitions:
        for row in reader.billing_candidates(partition):
            key = (row["region"], row["cluster_id"], row["machine_id"])
            rows, source_days = by_machine.setdefault(key, ([], set()))
            rows.append(row)
            source_days.add(partition)
    summaries, deferred = {}, []
    cutoff = minute(clock - timedelta(minutes=10))

    def summary(day):
        return summaries.setdefault(
            day,
            {"usage_date": day.isoformat(), "candidates": 0, "planned": 0,
             "horizon_gaps": 0, "errors": []},
        )

    merged, detected = [], {}
    for (region, cluster, machine_id), (rows, source_days) in by_machine.items():
        try:
            merged.extend((row, source_days) for row in TencentReader._merge_snapshots(rows))
        except ValueError as error:
            start = min(row["observed_from"] for row in rows)
            end = max(row["observed_until"] for row in rows)
            last_day = policy.usage_date(end - timedelta(microseconds=1))
            until = max(
                store.deadline(state, {**row, "kind": "machine"}, policy.day_start(last_day), end)
                for row in rows
            )
            detected[store._machine_billing_key(region, cluster, machine_id)] = {
                "start": start.isoformat(), "end": end.isoformat(), "until": until.isoformat(),
                "bill_days": [day.isoformat() for day in sorted(source_days)],
            }
            summary(policy.usage_date(start))["errors"].append(
                {"machine_id": machine_id, "region": region, "error": str(error)}
            )
    conflicts, hold = store.reconcile_machine_billing_conflicts(
        engine, {day.isoformat() for day in partitions}, detected,
        scan_started=clock, now=now, dry_run=dry_run,
    )
    conflict_state = {"machine_billing_conflicts": conflicts, "machine_billing_hold_until": hold}
    for candidate, source_days in merged:
        start = minute(candidate["observed_from"], ceil=True)
        fee_end = minute(candidate["observed_until"])
        tail_start = max(start, cutoff)
        if tail_start < fee_end:
            deferred.append({
                "machine_id": candidate["machine_id"], "region": candidate["region"],
                "cluster_id": candidate["cluster_id"], "start": tail_start.isoformat(),
                "end": fee_end.isoformat(), "bill_days": [day.isoformat() for day in sorted(source_days)],
            })
        end = min(fee_end, cutoff)
        if start >= end:
            continue
        floor = first_open[candidate["resource_class"]]
        if start < floor:
            # Report one gap for the unreplayable prefix instead of iterating
            # arbitrarily long expired/prepaid billing intervals.
            expired = summary(policy.usage_date(start))
            expired["candidates"] += 1
            expired["horizon_gaps"] += 1
            start = floor
        target = {**candidate, "kind": "machine", "account_id": policy.ACCOUNT_ID}
        while start < end:
            day = policy.usage_date(start)
            day_end = min(end, policy.day_start(day + timedelta(days=1)))
            result = summary(day)
            result["candidates"] += 1
            windows = []
            while start < day_end:
                boundary = start.replace(minute=start.minute // 30 * 30) + timedelta(minutes=30)
                stop = min(day_end, boundary)
                windows.append((target, start, stop))
                start = stop
            # A conflicting snapshot rolls back only this candidate's usage day.
            try:
                if reason := store._machine_bill_block(conflict_state, target, windows[0][1], day_end, clock):
                    raise ValueError(reason)
                outcome = store.enqueue_windows(engine, windows, now=now, dry_run=dry_run)
                result["planned"] += outcome["planned"]
            except ValueError as exc:
                result["errors"].append(
                    {"machine_id": target["machine_id"], "region": target["region"], "error": str(exc)}
                )
    return {
        "bill_days": [day.isoformat() for day in partitions],
        "days": [summaries[day] for day in sorted(summaries)],
        "deferred": deferred,
        "source": reader.stats,
        "dry_run": dry_run,
    }


def dispatch(engine, profile, producer, *, now=None, limit=100, dry_run=False):
    _ready(engine, profile, now)
    if dry_run:
        return {"dry_run": True, "sent": 0}
    planned = store.plan_contexts(engine, now=now, limit=limit)
    sent = 0
    for task in store.due_tasks(engine, now=now, limit=limit):
        producer.publish({"task_id": task["id"]})  # Broker acknowledgement before marking sent.
        store.mark_dispatched(engine, task["id"], now=now)
        sent += 1
    return {**planned, "sent": sent}


def work_batch(engine, profile, task_ids, *, now=None, reader=None):
    """Coalesce already available compatible tasks; never delay a consumer to fill a batch."""
    if not 1 <= len(task_ids) <= 10:
        raise ValueError("worker batches contain 1..10 task references")
    state, _ = _ready(engine, profile, now)
    reader = reader or TencentReader(profile)
    groups, results = {}, []
    for task_id in task_ids:
        task = store.claim_task(engine, task_id, now=now)
        if task is None:
            results.append("ignored")
            continue
        target = task["payload_json"]
        if target.get("contract_version") != state["contract_version"]:
            store.fail_task(engine, task["id"], task["lease_token"], now=now)
            results.append("retry")
            continue
        key = (
            task["window_start"],
            task["window_end"],
            task["kind"],
            target["region"],
            target.get("cluster_id", ""),
            target.get("resource_class", "pod"),
            # Pod queries have exact selectors, not a provider-side batch. An
            # unresolved Pending Pod must not poison a healthy Pod's attempt.
            target.get("pod_uid", ""),
        )
        groups.setdefault(key, []).append(task)
    for (start, end, *_), batch in groups.items():
        try:
            points = reader.window(start, end, [task["payload_json"] for task in batch])
        except Exception as exc:  # noqa: BLE001 -- durable retry, no source payload in logs
            LOG.warning("utilization source attempt failed: %s", type(exc).__name__)
            for task in batch:
                store.fail_task(engine, task["id"], task["lease_token"], now=now)
                results.append("retry")
            continue
        for task in batch:
            target = task["payload_json"]
            entity = target.get("machine_id") or target["pod_uid"]
            own = [point for point in points if point["entity"] == entity]
            if not any(point.get("value") is not None for point in own):
                store.fail_task(engine, task["id"], task["lease_token"], now=now)
                results.append("retry")
                continue
            try:
                values = summarize_window(own, target, start, end)
                done = store.complete_task(engine, task["id"], task["lease_token"], values, now=now)
            except ValueError:
                LOG.warning("utilization task result rejected")
                store.fail_task(engine, task["id"], task["lease_token"], now=now)
                results.append("retry")
            else:
                results.append("done" if done else "fenced")
    return results


def work_one(engine, profile, task_id, *, now=None, reader=None):
    return work_batch(engine, profile, [task_id], now=now, reader=reader)[0]


def consume(engine, profile, consumer, *, max_messages=100, now=None, reader=None):
    if not 1 <= max_messages <= 1000:
        raise ValueError("bounded worker batch required")
    state, clock = _ready(engine, profile, now)
    store._admit(state, clock)
    reader = reader or TencentReader(profile)
    counts = {"done": 0, "ignored": 0, "retry": 0, "fenced": 0, "invalid": 0}
    processed = 0
    while processed < max_messages:
        batches = consumer.poll(timeout_ms=1000, max_records=min(10, max_messages - processed))
        if not batches:
            break
        references = []
        for records in batches.values():
            for record in records:
                message = record.value
                try:
                    if isinstance(message, (str, bytes)):
                        if len(message) > 256:
                            raise ValueError("oversized task reference")
                        message = json.loads(message)
                    key = message["task_id"]
                    if (
                        set(message) != {"task_id"}
                        or len(key) != 64
                        or any(c not in "0123456789abcdef" for c in key)
                    ):
                        raise ValueError("invalid task reference")
                except (ValueError, KeyError, TypeError):
                    counts["invalid"] += 1
                else:
                    references.append(key)
                processed += 1
        for offset in range(0, len(references), 10):
            for result in work_batch(
                engine, profile, references[offset : offset + 10], now=now, reader=reader
            ):
                counts[result] += 1
        # A DB failure escapes before this commit; all fetched references have
        # either an idempotent committed result or a durable retry disposition.
        consumer.commit()
    return {"processed": processed, **counts}
