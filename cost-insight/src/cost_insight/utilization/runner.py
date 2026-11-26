"""Tencent-only planners and Kafka workers. Timers live in DB, never per-Pod threads."""

import json
import logging
from datetime import timedelta

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


def _horizon_open(state, day, clock):
    for cls in ("cvm", "tke_native"):
        for entry in state["contracts"][cls].values():
            policy.validate_source_window(entry)
            if clock >= policy.day_start(day) + timedelta(
                seconds=entry["verified_horizon_seconds"] - entry.get("safety_margin_seconds", 0)
            ):
                return False
    return True


def plan_machines(engine, profile, *, usage_date=None, dry_run=False, now=None, reader=None):
    state, clock = _ready(engine, profile, now)
    if not dry_run:
        store._admit(state, clock)
    days = (
        [usage_date]
        if usage_date
        else [policy.usage_date(clock) - timedelta(days=n) for n in (4, 3)]
    )
    reader = reader or TencentReader(profile)
    reader.verify_account()
    result = []
    for day in days:
        if day > policy.usage_date(clock) or day < policy.usage_date(clock) - timedelta(days=365):
            raise ValueError("machine date outside retained year")
        # Reject exhausted sources before doing a potentially large bill scan.
        if not _horizon_open(state, day, clock):
            if usage_date is not None:
                raise ValueError("machine date exceeds verified source horizon")
            result.append({"usage_date": day.isoformat(), "skipped": "source_horizon"})
            continue
        candidates = reader.billing_candidates(day)
        planned, errors = 0, []
        for candidate in candidates:
            windows = []
            target = {**candidate, "kind": "machine", "account_id": policy.ACCOUNT_ID}
            start = max(policy.day_start(day), minute(candidate["observed_from"], ceil=True))
            end = min(
                policy.day_start(day + timedelta(days=1)),
                minute(candidate["observed_until"]),
                minute(clock - timedelta(minutes=10)),
            )
            while start < end:
                stop = min(
                    end, start.replace(minute=start.minute // 30 * 30) + timedelta(minutes=30)
                )
                windows.append((target, start, stop))
                start = stop
            # One candidate has at most 48 windows. A revised immutable snapshot
            # must not block unrelated machines or the next usage day.
            try:
                planned += store.enqueue_windows(engine, windows, now=now, dry_run=dry_run)[
                    "planned"
                ]
            except ValueError as exc:
                errors.append(
                    {
                        "machine_id": target["machine_id"],
                        "region": target["region"],
                        "error": str(exc),
                    }
                )
        result.append(
            {
                "usage_date": day.isoformat(),
                "candidates": len(candidates),
                "planned": planned,
                "errors": errors,
            }
        )
    return {"days": result, "source": reader.stats, "dry_run": dry_run}


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
