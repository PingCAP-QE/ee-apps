"""Utilization orchestration: bounded transient windows, atomic daily replacement."""

from __future__ import annotations

import copy
from datetime import date, timedelta

from sqlalchemy import text

from cost_insight.utilization import policy, store
from cost_insight.utilization.aggregate import aggregate_day, day_bounds, resource_class, utc
from cost_insight.utilization.tencent import TencentReader


def database_now(engine, now=None):
    if now is not None:
        if engine.dialect.name != "sqlite":
            raise ValueError("Clock injection is only supported in offline SQLite tests")
        return utc(now)
    with engine.connect() as connection:
        statement = (
            "SELECT CURRENT_TIMESTAMP"
            if engine.dialect.name == "sqlite"
            else "SELECT UTC_TIMESTAMP(6)"
        )
        return utc(connection.execute(text(statement)).scalar_one())


def merge_window_rows(target: dict, rows: list[dict]) -> None:
    """Merge non-overlapping windows only; maxima remain synchronous minute means."""
    for row in rows:
        key = (row["scope"], row["resource_key"])
        if key not in target:
            target[key] = copy.deepcopy(row)
            continue
        existing = target[key]
        if row["cutoff"] <= existing["cutoff"]:
            raise ValueError("Aggregation windows must be ordered and non-overlapping")
        existing["cutoff"] = row["cutoff"]
        existing["status"] = row["status"]
        if existing["resource_spec"] != row["resource_spec"]:
            existing["resource_spec"] = "mixed"
        for metric, body in row["metrics_json"].items():
            if metric not in existing["metrics_json"]:
                existing["metrics_json"][metric] = copy.deepcopy(body)
                continue
            previous = existing["metrics_json"][metric]
            if previous["contract"] != body["contract"]:
                raise ValueError("Cannot merge incompatible daily window contracts")
            previous["usage_observed_seconds"] = previous.get(
                "usage_observed_seconds", previous["observed_seconds"]
            ) + body.get("usage_observed_seconds", body["observed_seconds"])
            for field in (
                "used_integral",
                "capacity_integral",
                "total_used_integral",
                "expected_capacity_integral",
                "expected_seconds",
                "observed_seconds",
                "unknown_seconds",
                "zero_capacity_seconds",
            ):
                previous[field] = previous.get(field, 0) + body.get(field, 0)
            previous["avg"] = (
                100 * previous["used_integral"] / previous["capacity_integral"]
                if previous["capacity_integral"]
                else None
            )
            previous["coverage"] = (
                min(
                    previous["capacity_integral"] / previous["expected_capacity_integral"]
                    if previous["expected_capacity_integral"]
                    else 1.0,
                    previous["observed_seconds"] / previous["expected_seconds"],
                )
                if previous["expected_seconds"]
                else None
            )
            if body["peak"] is not None and (
                previous["peak"] is None or body["peak"] > previous["peak"]
            ):
                previous.update({f: body[f] for f in ("peak", "peak_at", "peak_coverage")})
            if body.get("peak_subset") is not None and (
                previous.get("peak_subset") is None or body["peak_subset"] > previous["peak_subset"]
            ):
                previous.update(
                    {
                        field: body.get(field)
                        for field in ("peak_subset", "peak_subset_at", "peak_subset_coverage")
                    }
                )
            if body["as_of"] is not None:
                previous.update({f: body[f] for f in ("latest", "as_of")})
            previous["quality"] = sorted(set(previous["quality"]) | set(body["quality"]))


def sync_inventory(engine, profile, *, dry_run=False, now=None, reader=None):
    now = database_now(engine, now)
    if not dry_run:
        state = store.load_state(engine)
        if not policy.health(state, policy.utc_naive(now))["allowed"]:
            raise ValueError(
                "Utilization writes are disabled/unhealthy; maintenance must run first"
            )
    reader = reader or TencentReader(profile)
    reader.verify_account()
    rows = reader.inventory(now)
    results = [
        store.save_observations(
            engine,
            rows[offset : offset + 1000],
            now=policy.utc_naive(now) if engine.dialect.name == "sqlite" else None,
            dry_run=dry_run,
        )
        for offset in range(0, len(rows), 1000)
    ]
    return {
        "inventory": {
            "rows": sum(result["rows"] for result in results),
            "batches": len(results),
            "dry_run": dry_run,
        },
        "source": reader.stats,
    }


def sync_daily(
    engine,
    profile,
    *,
    dry_run=False,
    usage_date: date | None = None,
    repair=False,
    now=None,
    reader=None,
    empty_proof: dict | None = None,
):
    now = database_now(engine, now)
    if empty_proof is not None and (usage_date is None or not repair):
        raise ValueError("Verified empty proof requires an explicit --date --repair")
    state = store.load_state(engine)
    if not state or state.get("contracts") != profile["contracts"]:
        raise ValueError("Initialize an exact, disabled contract registry before collection")
    if not dry_run and not policy.health(state, policy.utc_naive(now))["allowed"]:
        raise ValueError("Utilization writes are disabled/unhealthy; maintenance must run first")
    today = now.astimezone(policy.BEIJING).date()
    if usage_date is not None and not repair:
        raise ValueError(
            "Explicit dates require --repair; regular refresh only recuts current/previous day"
        )
    days = [usage_date] if usage_date else [today - timedelta(days=1), today]
    reader = reader or TencentReader(profile)
    reader.verify_account()
    results = []
    # One common cutoff prevents different scopes seeing different prefixes.
    cutoff = (now - timedelta(minutes=10)).replace(second=0, microsecond=0)
    if empty_proof is not None:
        if empty_proof.get("usage_date") != usage_date.isoformat() or len(str(empty_proof)) > 8192:
            raise ValueError("Empty proof must be bounded and match the selected date")
        proof_cutoff = utc(empty_proof["cutoff"])
        if proof_cutoff > cutoff or proof_cutoff.second or proof_cutoff.microsecond:
            raise ValueError("Empty proof cannot exceed the common completed-minute cutoff")
        cutoff = proof_cutoff
    for day in days:
        if day > today or not policy.writable_week(
            state, policy.week_start(day), policy.utc_naive(now)
        ):
            if usage_date is not None:
                raise ValueError("Date is outside the explicitly open retention calendar")
            results.append(
                {
                    "usage_date": day.isoformat(),
                    "skipped": True,
                    "reason": "outside_collection_calendar",
                }
            )
            continue
        if cutoff <= utc(policy.day_start(day)):
            results.append(
                {"usage_date": day.isoformat(), "skipped": True, "reason": "no_completed_minutes"}
            )
            continue
        if not all(
            policy.replay_allowed(day, entry, policy.utc_naive(now))
            for entries in state["contracts"].values()
            for entry in entries.values()
        ):
            # Store retains the last trusted result; dry runs never mark it stale.
            outcome = (
                {"dry_run": True, "rows": 0}
                if dry_run
                else store.replace_day(
                    engine,
                    day,
                    [],
                    now=policy.utc_naive(now) if engine.dialect.name == "sqlite" else None,
                )
            )
            results.append(
                {
                    **outcome,
                    "usage_date": day.isoformat(),
                    "published": False,
                    "reason": "unverified_or_exhausted_source_horizon",
                }
            )
            continue
        start, end = day_bounds(day, cutoff)
        observations = store.read_observations(
            engine, policy.utc_naive(start), policy.utc_naive(end)
        )
        combined = {}
        cursor = start
        point_count = 0
        try:
            reader.verify_contracts()
            while cursor < end:
                window_end = min(end, cursor + timedelta(minutes=30))
                points = reader.window(cursor, window_end, observations)
                point_count += len(points)
                if empty_proof is not None:
                    for point in points:
                        if point["kind"] != "pod":
                            continue  # Store checks deduplicated physical rows, not alias series.
                        node = str(point.get("node_id") or "")
                        cls = resource_class(node) if node.startswith(("np-", "ins-")) else None
                        if cls == "tke_cvm" and profile["contracts"][cls]["cpu"].get(
                            "consumer_only"
                        ):
                            cls = "cvm"
                        if cls in empty_proof.get("resource_classes", []):
                            raise ValueError("Monitoring resource contradicts verified-empty scope")
                merge_window_rows(
                    combined,
                    aggregate_day(
                        day,
                        points,
                        observations,
                        profile["contracts"],
                        cutoff=window_end,
                        window_start=cursor,
                    ),
                )
                if len(combined) > store.MAX_DAY_ROWS:
                    raise ValueError(
                        "Daily stable-key cardinality exceeds canary publication ceiling"
                    )
                cursor = window_end
            if point_count == 0 and empty_proof is None:
                raise ValueError("No monitoring points; missing is not zero")
        except Exception as exc:  # noqa: BLE001 -- failure boundary: preserve last trusted day
            outcome = (
                {"dry_run": True, "rows": 0}
                if dry_run
                else store.replace_day(
                    engine,
                    day,
                    [],
                    now=policy.utc_naive(now) if engine.dialect.name == "sqlite" else None,
                )
            )
            results.append(
                {
                    **outcome,
                    "usage_date": day.isoformat(),
                    "published": False,
                    "reason": "collection_failed",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        rows = list(combined.values())
        unmapped = reader.stats.get("unmapped_node_series", 0) > 0
        for row in rows:
            if unmapped and row["scope"] in {"resource", "account_class"}:
                for body in row["metrics_json"].values():
                    body["quality"] = sorted(set(body["quality"]) | {"unmapped_physical_source"})
                    body["peak_coverage"] = None
            row.update(
                cutoff=policy.utc_naive(end),
                evidence_cutoff=policy.utc_naive(now),
                status="partial"
                if end < utc(policy.day_start(day + timedelta(days=1)))
                else "complete",
            )
        proof = (
            {**empty_proof, "source_quality": ["unmapped_physical_source"] if unmapped else []}
            if empty_proof is not None
            else None
        )
        results.append(
            store.replace_day(
                engine,
                day,
                rows,
                now=policy.utc_naive(now) if engine.dialect.name == "sqlite" else None,
                dry_run=dry_run,
                verified_empty=proof,
            )
        )
    return {
        "days": results,
        "source": reader.stats,
        "cutoff": cutoff.isoformat(),
        "dry_run": dry_run,
    }
