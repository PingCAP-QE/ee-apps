"""Bounded utilization persistence, guarded by one existing cost_job_state row.

Guard protocol v1 is shared with ci-dashboard's metadata sink. Acquire a
pessimistic transaction, lock GUARD_KEY, THEN read DB time and guard JSON. All
short-lived inserts/deletes and retained_rows deltas commit with that guard.
The injected clock is deliberately available only on SQLite for offline tests.
No function creates tables, contacts a cloud, or initializes state on a dry run.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.engine import Connection, Engine

from cost_insight.jobs.state_store import checkpoint_job_watermark
from cost_insight.utilization import policy
from cost_insight.utilization.schema import OBSERVATION_ATTRIBUTES, daily, observations, weekly

MAX_DAY_ROWS = 10_000
MAX_DAY_BYTES = 16 * 1024**2
MAX_TRANSACTION_SECONDS = 10
IDENTITY = {"vendor": policy.VENDOR, "account_id": policy.ACCOUNT_ID}
HARD_QUALITY = frozenset(
    {
        "contract_mismatch",
        "missing_days",
        "stale",
        "partial",
        "unknown_capacity",
        "invalid_physical_capacity",
        "unmapped_node_inventory",
        "unmapped_physical_source",
    }
)


@contextmanager
def _transaction(engine):
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


def _clock(connection, now=None):
    if now is not None:
        if connection.dialect.name != "sqlite":
            raise ValueError("clock override is restricted to offline SQLite tests")
        return policy.utc_naive(now)
    sql = (
        "SELECT CURRENT_TIMESTAMP"
        if connection.dialect.name == "sqlite"
        else "SELECT UTC_TIMESTAMP(6)"
    )
    return policy.utc_naive(connection.execute(text(sql)).scalar_one())


def _read_state(connection, *, lock=False):
    suffix = " FOR UPDATE" if lock and connection.dialect.name != "sqlite" else ""
    value = connection.execute(
        text("SELECT watermark_json FROM cost_job_state WHERE job_name = :key" + suffix),
        {"key": policy.GUARD_KEY},
    ).scalar()
    if value is None:
        return {}
    state = value if isinstance(value, dict) else json.loads(value)
    if not isinstance(state, dict):
        raise TypeError("invalid utilization guard")
    return state


def load_state(connection_or_engine: Connection | Engine) -> dict:
    if isinstance(connection_or_engine, Engine):
        with connection_or_engine.connect() as connection:
            return _read_state(connection)
    return _read_state(connection_or_engine)


def _save_state(connection, state):
    checkpoint_job_watermark(connection, policy.GUARD_KEY, state)


def _account(table):
    return and_(table.c.vendor == policy.VENDOR, table.c.account_id == policy.ACCOUNT_ID)


def configure(
    engine, *, start_week, contracts, enabled=False, dry_run=False, now=None, stop_week=None
) -> dict:
    start_week = date.fromisoformat(start_week) if isinstance(start_week, str) else start_week
    stop_week = date.fromisoformat(stop_week) if isinstance(stop_week, str) else stop_week
    if stop_week is not None and (
        stop_week != policy.week_start(stop_week) or stop_week < start_week
    ):
        raise ValueError("stop_week must be an inclusive Monday >= start_week")
    if start_week != policy.week_start(start_week) or not contracts or len(contracts) > 64:
        raise ValueError("a Monday and 1..64 explicit resource class contracts are required")
    for metrics in contracts.values():
        if not metrics:
            raise ValueError("empty metric scope")
        for metric, entry in metrics.items():
            policy.validate_source_window(entry)
            policy.validate_contract(entry["contract"], metric, "account_class")
            for scope, contract in entry.get("scope_contracts", {}).items():
                if scope not in {"resource", "job", "repo"}:
                    raise ValueError("invalid contract scope")
                policy.validate_contract(contract, metric, scope)
    if len(json.dumps(contracts)) > 64 * 1024:
        raise ValueError("contracts exceed bounded guard payload")
    if dry_run:
        return {"dry_run": True, "state": load_state(engine)}
    with _transaction(engine) as connection:
        state = _read_state(connection, lock=True)
        _clock(connection, now)
        if state:
            if (
                state["collection_start_week"] != start_week.isoformat()
                or state["contracts"] != contracts
            ):
                raise ValueError(
                    "collection calendar/contracts are immutable; schedule changes explicitly"
                )
            state["enabled"] = bool(enabled)
            if stop_week is not None:
                state["collection_stop_week"] = stop_week.isoformat()
            _save_state(connection, state)
        else:
            state = {
                "version": 1,
                "enabled": bool(enabled),
                "collection_start_week": start_week.isoformat(),
                "collection_stop_week": stop_week.isoformat() if stop_week else None,
                "contracts": contracts,
                "finalized_through_week": None,
                "retained_rows": 0,
                "counts_reliable": False,
                "observed_bytes": None,
                "bytes_observed_at": None,
                "maintenance_at": None,
                "oldest_expired_at": None,
                "reconciled_at": None,
                "paused": True,
                "pause_reasons": ["maintenance_uninitialized"],
            }
            # INSERT only: a concurrent first configuration must not reset counters/state.
            connection.execute(
                text("""
                INSERT INTO cost_job_state (job_name, watermark_json, last_status, updated_at)
                VALUES (:key, :state, 'configured', CURRENT_TIMESTAMP)
            """),
                {"key": policy.GUARD_KEY, "state": json.dumps(state, sort_keys=True)},
            )
        return dict(state)


def _admit(state, now, weeks, projected_rows):
    result = policy.health(state, now, projected_rows=projected_rows)
    if not result["allowed"]:
        raise ValueError("utilization ingestion blocked: " + ",".join(result["reasons"]))
    if any(not policy.writable_week(state, week, now) for week in weeks):
        raise ValueError("usage week is expired, closed, or outside collection calendar")


def _bounded_string(value, name, limit, *, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not value and not empty):
        raise ValueError(f"invalid {name}; never truncate identity")
    return value


def _consumer_totals(rows):
    totals = {}
    for row in rows:
        if row["scope"] not in {"job", "repo"}:
            continue
        for metric, body in row["metrics_json"].items():
            # The all-class key permits unknown -> known enrichment without
            # allowing lost measurements to disappear from the consumer scope.
            for cls in (row["resource_class"], None):
                key = (row["scope"], cls, metric)
                value = totals.setdefault(key, [0, 0, 0])
                value[0] += body["expected_seconds"]
                value[1] += body["observed_seconds"]
                value[2] += body.get("usage_observed_seconds", body["observed_seconds"])
    return totals


def _metric_body(body, expected, metric, scope):
    policy.validate_contract(body["contract"], metric, scope)
    if body["contract"] != expected:
        raise ValueError("metric contract mismatch")
    for field in (
        "used_integral",
        "capacity_integral",
        "total_used_integral",
        "expected_capacity_integral",
        "expected_seconds",
        "observed_seconds",
    ):
        value = body[field]
        if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid metric integral")
    for field in ("unknown_seconds", "zero_capacity_seconds", "usage_observed_seconds"):
        value = body.get(field, 0)
        if (
            not isinstance(value, (float, int))
            or not math.isfinite(value)
            or not 0 <= value <= body["expected_seconds"]
        ):
            raise ValueError("invalid metric entity-seconds")
    for field in ("coverage", "peak_coverage", "peak_subset_coverage"):
        value = body.get(field)
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError("invalid metric coverage")
    for field in ("avg", "peak", "peak_subset", "latest"):
        value = body.get(field)
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("invalid metric percentage")
    if body["capacity_integral"] == 0 and body.get("avg") is not None:
        raise ValueError("zero denominator requires NULL utilization")
    allowed = {
        "contract",
        "used_integral",
        "capacity_integral",
        "total_used_integral",
        "expected_capacity_integral",
        "expected_seconds",
        "observed_seconds",
        "unknown_seconds",
        "zero_capacity_seconds",
        "usage_observed_seconds",
        "avg",
        "peak",
        "peak_at",
        "coverage",
        "peak_coverage",
        "peak_subset",
        "peak_subset_at",
        "peak_subset_coverage",
        "latest",
        "as_of",
        "quality",
    }
    quality = body.get("quality", [])
    if (
        not isinstance(quality, list)
        or len(quality) > 32
        or any(not isinstance(q, str) or len(q) > 80 for q in quality)
    ):
        raise ValueError("invalid bounded quality flags")
    if body.keys() - allowed or len(json.dumps(body, allow_nan=False)) > 8192:
        raise ValueError("unbounded or unknown metric fields")


def _prepare_day(usage_date, rows, state, now):
    week = policy.week_start(usage_date)
    prepared = []
    if len(rows) > MAX_DAY_ROWS or len(json.dumps(rows, default=str)) > MAX_DAY_BYTES:
        raise ValueError("day exceeds bounded publication ceiling; canary review required")
    for row in rows:
        scope, resource_class = row["scope"], row["resource_class"]
        if scope not in {"resource", "job", "repo", "account_class"}:
            raise ValueError("invalid daily scope")
        if any(row.get(key, value) != value for key, value in IDENTITY.items()):
            raise ValueError("wrong utilization account")
        metrics = row["metrics_json"]
        if not metrics or metrics.keys() - policy.METRICS:
            raise ValueError("invalid metric groups")
        for metric, body in metrics.items():
            entry = state["contracts"][resource_class][metric]
            if entry.get("consumer_only") and scope not in {"job", "repo"}:
                raise ValueError("consumer-only class cannot publish physical scopes")
            expected = policy.expected_contract(state["contracts"], resource_class, metric, scope)
            _metric_body(body, expected, metric, scope)
            if not policy.replay_allowed(usage_date, entry, now):
                raise ValueError("unverified or exhausted fine-grained source horizon")
        cutoff = policy.utc_naive(row["cutoff"])
        if (
            not policy.day_start(usage_date)
            <= cutoff
            <= min(now, policy.day_start(usage_date + timedelta(days=1)))
        ):
            raise ValueError("invalid day cutoff")
        status = row.get("status", "partial")
        if status not in {"partial", "complete", "stale"}:
            raise ValueError("invalid daily status")
        item = dict(
            IDENTITY,
            usage_date=usage_date,
            week_start=week,
            expires_at=policy.expires_at(week),
            scope=scope,
            metrics_json=metrics,
            cutoff=cutoff,
            status=status,
            updated_at=now,
            evidence_cutoff=policy.utc_naive(row["evidence_cutoff"])
            if row.get("evidence_cutoff")
            else None,
        )
        for field, limit, default in (
            ("resource_key", 64, ""),
            ("resource_class", 64, ""),
            ("resource_spec", 255, ""),
            ("repo", 255, "unknown_repo"),
            ("job_name", 512, "unknown_job"),
            ("namespace", 128, ""),
            ("region", 64, ""),
        ):
            item[field] = _bounded_string(
                row.get(field, default),
                field,
                limit,
                empty=field not in {"resource_key", "resource_class"},
            )
        if len(item["resource_key"]) != 64 or any(
            c not in "0123456789abcdef" for c in item["resource_key"]
        ):
            raise ValueError("resource_key must be sha256")
        if item["evidence_cutoff"] and item["evidence_cutoff"] > now:
            raise ValueError("evidence cutoff cannot be in the future")
        prepared.append(item)
    return prepared


def _verified_empty_rows(usage_date, rows, proof, state, now):
    for row in rows:
        for body in row["metrics_json"].values():
            if any(
                isinstance(q, str) and (q == "verified_empty" or q.startswith("proof_sha256:"))
                for q in body.get("quality", [])
            ):
                raise ValueError("verified-empty quality is reserved for explicit proof")
    if proof is None:
        return []
    if not state or not policy.writable_week(state, policy.week_start(usage_date), now):
        raise ValueError("verified-empty week is expired, closed, or outside collection calendar")
    required = {
        "vendor",
        "account_id",
        "usage_date",
        "cutoff",
        "resource_classes",
        "source_scope_hash",
        "evidence_reference",
        "verified_at",
        "confirmed_empty",
        "source_quality",
    }
    if not isinstance(proof, dict) or proof.keys() != required:
        raise ValueError("invalid verified-empty proof fields")
    if len(json.dumps(proof, allow_nan=False)) > 8192:
        raise ValueError("verified-empty proof exceeds bounded payload")
    if any(proof[key] != value for key, value in IDENTITY.items()):
        raise ValueError("wrong verified-empty account")
    if proof["usage_date"] != usage_date.isoformat() or proof["confirmed_empty"] is not True:
        raise ValueError("verified-empty proof must confirm the selected day")
    scope_hash = _bounded_string(proof["source_scope_hash"], "source_scope_hash", 64, empty=False)
    if len(scope_hash) != 64 or any(c not in "0123456789abcdef" for c in scope_hash):
        raise ValueError("source_scope_hash must be sha256")
    _bounded_string(proof["evidence_reference"], "evidence_reference", 255, empty=False)
    times = {}
    for key in ("cutoff", "verified_at"):
        value = _bounded_string(proof[key], key, 40, empty=False)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("verified-empty timestamps must be UTC ISO")
        times[key] = policy.utc_naive(parsed)
    cutoff, verified_at = times["cutoff"], times["verified_at"]
    end = policy.day_start(usage_date + timedelta(days=1))
    if not (
        policy.day_start(usage_date) < cutoff <= min(now, end)
        and cutoff <= verified_at <= now
        and (now < end or cutoff == end)
    ):
        raise ValueError("invalid verified-empty cutoff/verification time")
    classes, quality = proof["resource_classes"], proof["source_quality"]
    if (
        not isinstance(classes, list)
        or not 1 <= len(classes) <= 64
        or any(not isinstance(cls, str) for cls in classes)
        or len(set(classes)) != len(classes)
    ):
        raise ValueError("invalid verified-empty resource classes")
    if (
        not isinstance(quality, list)
        or len(quality) > 2
        or any(q not in {"unmapped_physical_source", "unmapped_node_inventory"} for q in quality)
        or len(set(quality)) != len(quality)
    ):
        raise ValueError("invalid verified-empty source quality")
    digest = hashlib.sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    result = []
    for cls in classes:
        metrics = state["contracts"].get(cls)
        if not metrics or any(entry.get("consumer_only") for entry in metrics.values()):
            raise ValueError("verified-empty class must be configured physical inventory")
        if any(
            r["resource_class"] == cls and r["scope"] in {"resource", "account_class"} for r in rows
        ):
            raise ValueError("verified-empty class contradicts physical daily rows")
        bodies = {}
        for metric, entry in metrics.items():
            if entry["contract"].get("source_scope_hash") != scope_hash:
                raise ValueError("verified-empty source scope hash mismatch")
            bodies[metric] = {
                "contract": entry["contract"],
                **dict.fromkeys(
                    (
                        "used_integral",
                        "capacity_integral",
                        "total_used_integral",
                        "expected_capacity_integral",
                        "expected_seconds",
                        "observed_seconds",
                        "unknown_seconds",
                        "zero_capacity_seconds",
                    ),
                    0,
                ),
                "avg": None,
                "peak": None,
                "peak_at": None,
                "latest": None,
                "coverage": 1,
                "peak_coverage": None,
                "as_of": cutoff.isoformat(),
                "quality": ["verified_empty", "proof_sha256:" + digest, *quality],
            }
        key = hashlib.sha256(
            json.dumps(("account_class", cls), separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        result.append(
            {
                "scope": "account_class",
                "resource_class": cls,
                "resource_key": key,
                "metrics_json": bodies,
                "cutoff": cutoff,
                "evidence_cutoff": verified_at,
                "status": "complete" if cutoff == end else "partial",
            }
        )
    return result


def replace_day(
    engine, usage_date, rows: list[dict], *, now=None, dry_run=False, verified_empty=None
) -> dict:
    usage_date = date.fromisoformat(usage_date) if isinstance(usage_date, str) else usage_date
    if dry_run:
        with engine.connect() as connection:
            state = _read_state(connection)
            clock = _clock(connection, now)
            candidates = rows + _verified_empty_rows(usage_date, rows, verified_empty, state, clock)
            if not candidates:
                raise ValueError("zero rows is not evidence of an empty account")
            prepared = _prepare_day(usage_date, candidates, state, clock)
        return {"dry_run": True, "rows": len(prepared)}
    started = time.monotonic()
    with _transaction(engine) as connection:
        state = _read_state(connection, lock=True)
        clock = _clock(connection, now)
        week = policy.week_start(usage_date)
        old_count = connection.execute(
            select(func.count())
            .select_from(daily)
            .where(_account(daily), daily.c.usage_date == usage_date)
        ).scalar_one()
        _admit(state, clock, [week], state.get("retained_rows", 0) - old_count + len(rows))
        try:
            candidates = rows + _verified_empty_rows(usage_date, rows, verified_empty, state, clock)
            if not candidates:
                raise ValueError("zero rows is not evidence of an empty account")
            prepared = _prepare_day(usage_date, candidates, state, clock)
            cutoffs = {item["cutoff"] for item in prepared}
            if len(cutoffs) != 1:
                raise ValueError("daily scopes must share one common cutoff")
            old_cutoff = connection.execute(
                select(func.max(daily.c.cutoff)).where(
                    _account(daily), daily.c.usage_date == usage_date
                )
            ).scalar_one()
            if old_cutoff and next(iter(cutoffs)) < old_cutoff:
                return {"published": False, "reason": "older_daily_cutoff", "rows": old_count}
            prior = (
                connection.execute(
                    select(daily)
                    .where(
                        _account(daily),
                        daily.c.usage_date == usage_date,
                        daily.c.scope == "account_class",
                    )
                    .limit(65)
                )
                .mappings()
                .all()
            )
            if len(prior) > 64:
                raise ValueError("prior physical scopes exceed publication ceiling")
            incoming = {
                item["resource_class"]: item["metrics_json"]
                for item in prepared
                if item["scope"] == "account_class"
            }
            for fact in prior:
                if (
                    fact["resource_class"] not in incoming
                    and fact["cutoff"]
                    == next(iter(cutoffs))
                    == policy.day_start(usage_date + timedelta(days=1))
                    and all(
                        "verified_empty" in body.get("quality", [])
                        for body in fact["metrics_json"].values()
                    )
                ):
                    # A previously proved empty *whole day* remains proved when a
                    # fresh read has no rows. Never extend a partial empty proof.
                    retained = {key: value for key, value in fact.items() if key != "id"}
                    retained.update(updated_at=clock, status="complete")
                    prepared.append(retained)
                    incoming[fact["resource_class"]] = fact["metrics_json"]
                for metric, body in fact["metrics_json"].items():
                    replacement = incoming.get(fact["resource_class"], {}).get(metric, {})
                    if any(
                        replacement.get(field, 0) < body.get(field, 0)
                        for field in ("expected_seconds", "observed_seconds")
                    ):
                        raise ValueError(
                            "physical evidence regressed; keep last trusted daily values"
                        )
            prior_consumers = (
                connection.execute(
                    select(daily.c.scope, daily.c.resource_class, daily.c.metrics_json)
                    .where(
                        _account(daily),
                        daily.c.usage_date == usage_date,
                        daily.c.scope.in_(["job", "repo"]),
                    )
                    .limit(MAX_DAY_ROWS + 1)
                )
                .mappings()
                .all()
            )
            if len(prior_consumers) > MAX_DAY_ROWS:
                raise ValueError("prior consumer scopes exceed publication ceiling")
            old_totals, new_totals = _consumer_totals(prior_consumers), _consumer_totals(prepared)
            for key, old in old_totals.items():
                if key[1] == "tke_unknown":
                    continue  # Transfers are checked by the all-class key.
                new = new_totals.get(key, [0, 0, 0])
                if any(current < previous for current, previous in zip(new, old, strict=True)):
                    raise ValueError("consumer evidence regressed; keep last trusted daily values")
            if (
                len(prepared) > MAX_DAY_ROWS
                or len(json.dumps(prepared, default=str)) > MAX_DAY_BYTES
            ):
                raise ValueError("retained proofs exceed daily publication ceiling")
        except (ValueError, KeyError, TypeError) as exc:
            connection.execute(
                daily.update()
                .where(_account(daily), daily.c.usage_date == usage_date)
                .values(status="stale", updated_at=clock)
            )
            _refresh_week(connection, state, week, clock)
            return {"published": False, "reason": str(exc), "rows": old_count}
        _admit(state, clock, [week], state["retained_rows"] - old_count + len(prepared))
        connection.execute(daily.delete().where(_account(daily), daily.c.usage_date == usage_date))
        connection.execute(daily.insert(), prepared)
        state["retained_rows"] += len(prepared) - old_count
        _refresh_week(connection, state, week, clock)
        # A writer admitted before midnight must not commit past the hard fence.
        _admit(state, _clock(connection, now), [week], state["retained_rows"])
        _save_state(connection, state)
        duration = time.monotonic() - started
        if duration > MAX_TRANSACTION_SECONDS:
            raise ValueError("daily publication exceeded transaction time ceiling")
        return {
            "published": True,
            "rows": len(prepared),
            "replaced_rows": old_count,
            "transaction_seconds": duration,
            "publication_bytes": len(json.dumps(prepared, default=str).encode()),
        }


def _refresh_week(connection, state, week, now, *, final=False, checkpoint=False):
    # Guard is already held: the finalizer fences writers BEFORE reading daily facts.
    facts = (
        connection.execute(
            select(daily.c.usage_date, daily.c.resource_class, daily.c.metrics_json, daily.c.status)
            .where(_account(daily), daily.c.week_start == week, daily.c.scope == "account_class")
            .limit(64 * 7 + 1)
            .with_for_update()
        )
        .mappings()
        .all()
    )
    if len(facts) > 64 * 7:
        raise ValueError("account_class input exceeds bounded weekly scope")
    existing = {
        (r["resource_class"], r["metric"]): r
        for r in connection.execute(
            select(weekly).where(_account(weekly), weekly.c.week_start == week).with_for_update()
        ).mappings()
    }
    for resource_class, metrics in state["contracts"].items():
        for metric, entry in metrics.items():
            if entry.get("consumer_only"):
                continue
            expected = entry["contract"]
            valid, days, quality = [], set(), set()
            for fact in facts:
                if fact["resource_class"] != resource_class:
                    continue
                body = fact["metrics_json"].get(metric)
                if body is None:
                    continue
                try:
                    _metric_body(body, expected, metric, "account_class")
                except (ValueError, KeyError, TypeError):
                    quality.add("contract_mismatch")
                    continue
                if fact["usage_date"] in days:
                    raise ValueError("duplicate account_class daily scope")
                days.add(fact["usage_date"])
                valid.append(body)
                quality.update(body.get("quality", []))
                if fact["status"] != "complete":
                    quality.add(fact["status"])
            fields = (
                "used_integral",
                "capacity_integral",
                "total_used_integral",
                "expected_capacity_integral",
                "expected_seconds",
                "observed_seconds",
            )
            values = {field: sum(b[field] for b in valid) if valid else None for field in fields}
            for field in ("unknown_seconds", "zero_capacity_seconds"):
                values[field] = sum(b.get(field, 0) for b in valid) if valid else None
            values["usage_observed_seconds"] = (
                sum(b.get("usage_observed_seconds", b["observed_seconds"]) for b in valid)
                if valid
                else None
            )
            # These are entity-seconds, NOT wall-clock seconds. Ratios remain numeric
            # despite quality flags; an absent calendar day still has no denominator.
            expected_seconds = values["expected_seconds"]
            expected_capacity = values["expected_capacity_integral"]
            coverage = (
                min(
                    1.0,
                    values["observed_seconds"] / expected_seconds,
                    values["capacity_integral"] / expected_capacity if expected_capacity else 1,
                )
                if expected_seconds
                else None
            )
            if (
                valid
                and not expected_seconds
                and all(
                    b.get("coverage") == 1 and "verified_empty" in b.get("quality", [])
                    for b in valid
                )
            ):
                coverage = 1.0  # Explicitly verified empty scope; utilization stays NULL.
            values["observed_coverage"] = coverage
            values["expected_days"] = 7
            values["coverage"] = coverage if len(days) == 7 else None
            capacity = values["capacity_integral"]
            values["avg"] = 100 * values["used_integral"] / capacity if capacity else None
            peaks = [b for b in valid if b.get("peak") is not None and b.get("peak_coverage") == 1]
            peak = max(peaks, key=lambda b: b["peak"]) if peaks else {}
            values.update(
                peak=peak.get("peak"),
                peak_at=peak.get("peak_at"),
                peak_coverage=peak.get("peak_coverage"),
                observed_days=len(days),
            )
            subsets = [body for body in valid if body.get("peak_subset") is not None]
            subset = max(subsets, key=lambda body: body["peak_subset"]) if subsets else {}
            values.update(
                {
                    field: subset.get(field)
                    for field in ("peak_subset", "peak_subset_at", "peak_subset_coverage")
                }
            )
            if len(days) < 7:
                quality.add("missing_days")
            values["quality"] = sorted(quality)
            complete = (
                len(days) == 7
                and not quality.intersection(HARD_QUALITY)
                and values["coverage"] is not None
                and values["coverage"] >= 0.95
            )
            status = (
                ("final_complete" if complete else "final_incomplete" if valid else "final_missing")
                if final
                else "provisional"
            )
            previous = existing.get((resource_class, metric), {})
            item = dict(
                IDENTITY,
                week_start=week,
                resource_class=resource_class,
                metric=metric,
                contract_json=expected,
                values_json=values,
                status=status,
                quality_checkpoint_at=previous.get("quality_checkpoint_at")
                or (now if checkpoint else None),
                finalized_at=now if final else None,
                updated_at=now,
            )
            if previous:
                connection.execute(
                    weekly.update().where(weekly.c.id == previous["id"]).values(**item)
                )
            else:
                connection.execute(weekly.insert().values(**item))


def save_observations(engine, observations: list[dict], *, now=None, dry_run=False) -> dict:
    """Save already time-sliced evidence; no current snapshot is backdated/carry-forwarded."""
    if len(observations) > 1000:
        raise ValueError("observation batch exceeds 1000")
    if dry_run:
        load_state(engine)
        return {"dry_run": True, "rows": len(observations)}
    table = globals()["observations"]
    with _transaction(engine) as connection:
        state = _read_state(connection, lock=True)
        clock = _clock(connection, now)
        prepared = []
        for row in observations:
            if any(row.get(key, value) != value for key, value in IDENTITY.items()):
                raise ValueError("wrong utilization account")
            start = policy.utc_naive(row["observed_from"])
            week = policy.week_start(start)
            end = policy.utc_naive(row["observed_until"]) if row.get("observed_until") else None
            seen = policy.utc_naive(row.get("last_seen", start))
            if start > clock or seen > clock or seen < start or (end and not start <= end <= clock):
                raise ValueError("invalid observation interval")
            boundary = policy.day_start(week + timedelta(days=7))
            if start >= boundary or (end and end > boundary) or seen > boundary:
                raise ValueError("observation must be sliced at week boundary")
            attrs = row["attributes_json"]
            if (
                attrs.keys() - OBSERVATION_ATTRIBUTES
                or len(json.dumps(attrs, allow_nan=False)) > 8192
            ):
                raise ValueError("non-whitelisted or oversized evidence")
            for key in (
                "cpu_request",
                "memory_request",
                "cpu_limit",
                "memory_limit",
                "cpu_capacity",
                "memory_capacity",
                "allocatable_cpu",
                "allocatable_memory",
            ):
                if (
                    key in attrs
                    and attrs[key] is not None
                    and (
                        not isinstance(attrs[key], (float, int))
                        or attrs[key] < 0
                        or not math.isfinite(attrs[key])
                    )
                ):
                    raise ValueError("invalid evidence capacity")
            resource_type = row["resource_type"]
            if resource_type not in {"pod", "node", "cvm"}:
                raise ValueError("invalid resource type")
            item = dict(
                IDENTITY,
                resource_type=resource_type,
                week_start=week,
                expires_at=policy.expires_at(week),
                observed_from=start,
                observed_until=end,
                last_seen=seen,
                attributes_json=attrs,
                fingerprint=hashlib.sha256(json.dumps(attrs, sort_keys=True).encode()).hexdigest(),
            )
            for field, limit in (
                ("resource_id", 255),
                ("cluster", 128),
                ("namespace", 128),
                ("name", 255),
                ("provider_id", 255),
            ):
                item[field] = _bounded_string(
                    row.get(field, ""), field, limit, empty=field != "resource_id"
                )
            prepared.append(item)
        # Reserve a conservative upper bound; duplicates cannot exceed it.
        _admit(
            state,
            clock,
            [r["week_start"] for r in prepared],
            state.get("retained_rows", 0) + len(prepared),
        )
        inserted = 0
        for item in prepared:
            predicate = and_(
                _account(table),
                table.c.week_start == item["week_start"],
                table.c.resource_type == item["resource_type"],
                table.c.resource_id == item["resource_id"],
                table.c.cluster == item["cluster"],
                table.c.namespace == item["namespace"],
            )
            previous = (
                connection.execute(
                    select(table)
                    .where(predicate)
                    .order_by(table.c.observed_from.desc())
                    .limit(1)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if previous is None and item["resource_type"] == "cvm":
                # A fresh identical inventory sample confirms bounded continuity
                # across Monday; split it, never renew the preceding week's TTL.
                boundary = policy.day_start(item["week_start"])
                prior = (
                    connection.execute(
                        select(table)
                        .where(
                            _account(table),
                            table.c.week_start == item["week_start"] - timedelta(days=7),
                            table.c.resource_type == "cvm",
                            table.c.resource_id == item["resource_id"],
                            table.c.cluster == item["cluster"],
                            table.c.namespace == item["namespace"],
                        )
                        .order_by(table.c.observed_from.desc())
                        .limit(1)
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if (
                    prior
                    and prior["fingerprint"] == item["fingerprint"]
                    and prior["observed_until"] is None
                    and prior["last_seen"] < boundary <= item["observed_from"]
                    and item["observed_from"] - prior["last_seen"] <= timedelta(minutes=45)
                    and policy.writable_week(state, prior["week_start"], clock)
                ):
                    connection.execute(
                        table.update()
                        .where(table.c.id == prior["id"])
                        .values(observed_until=boundary, last_seen=boundary)
                    )
                    item["observed_from"] = boundary
            if previous and previous["observed_from"] > item["observed_from"]:
                raise ValueError("out-of-order evidence cannot rewrite the past")
            continuous = bool(
                previous and item["observed_from"] - previous["last_seen"] <= timedelta(minutes=45)
            )
            if previous and previous["last_seen"] > item["last_seen"]:
                raise ValueError("out-of-order evidence cannot move last_seen backwards")
            if (
                previous
                and previous["fingerprint"] == item["fingerprint"]
                and previous["observed_until"] is None
                and continuous
            ):
                connection.execute(
                    table.update()
                    .where(table.c.id == previous["id"])
                    .values(last_seen=item["last_seen"], observed_until=item["observed_until"])
                )
                continue
            if previous and previous["observed_from"] == item["observed_from"]:
                if previous["fingerprint"] != item["fingerprint"]:
                    raise ValueError("conflicting evidence at same instant")
                continue
            if previous and previous["observed_until"] is None:
                connection.execute(
                    table.update()
                    .where(table.c.id == previous["id"])
                    .values(
                        observed_until=item["observed_from"]
                        if continuous and item["resource_type"] != "cvm"
                        else previous["last_seen"]
                    )
                )
            connection.execute(table.insert().values(**item))
            inserted += 1
        state["retained_rows"] += inserted
        _admit(
            state,
            _clock(connection, now),
            [r["week_start"] for r in prepared],
            state["retained_rows"],
        )
        _save_state(connection, state)
        return {"rows": inserted}


def read_observations(engine, start, end, resource_type=None) -> list[dict]:
    start, end = policy.utc_naive(start), policy.utc_naive(end)
    if not start < end or end - start > timedelta(days=14):
        raise ValueError("evidence reads require a nonempty <=14 day interval")
    if resource_type is not None and resource_type not in {"pod", "node", "cvm"}:
        raise ValueError("invalid resource type")
    table = observations
    query = select(table).where(
        _account(table),
        table.c.week_start >= policy.week_start(start),
        table.c.week_start <= policy.week_start(end - timedelta(microseconds=1)),
        table.c.observed_from < end,
        or_(table.c.observed_until.is_(None), table.c.observed_until > start),
    )
    if resource_type:
        query = query.where(table.c.resource_type == resource_type)
    with engine.connect() as connection:
        # Bounded by retained week slices, with a hard fail rather than silent truncation.
        rows = connection.execute(query.order_by(table.c.id).limit(100_001)).mappings().all()
        if len(rows) > 100_000:
            raise ValueError("evidence read exceeds bounded window; split by type/time")
        return [dict(row) for row in rows]


def _oldest_expired(connection, now):
    values = [
        connection.execute(
            select(func.min(table.c.expires_at)).where(_account(table), table.c.expires_at <= now)
        ).scalar()
        for table in (daily, observations)
    ]
    return min((value for value in values if value is not None), default=None)


def _count_rows(connection):
    # Nightly bounded reconciliation; reaching the cap stays paused, never estimates low.
    return sum(
        connection.execute(
            select(func.count()).select_from(
                select(table.c.id).where(_account(table)).limit(policy.ROW_LIMIT + 1).subquery()
            )
        ).scalar_one()
        for table in (daily, observations)
    )


def _observed_bytes(connection):
    if connection.dialect.name == "sqlite":
        # dbstat reports table AND every index page, not only payload/index bytes.
        return int(
            connection.execute(
                text("""
            SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name IN (
              SELECT name FROM sqlite_master WHERE tbl_name IN
              ('cost_utilization_daily', 'cost_utilization_resource_observation')
            )
        """)
            ).scalar_one()
        )
    row = (
        connection.execute(
            text("""
        SELECT COUNT(*) AS n, COUNT(DATA_LENGTH + INDEX_LENGTH) AS measured_n,
               SUM(DATA_LENGTH + INDEX_LENGTH) AS bytes
        FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME IN ('cost_utilization_daily','cost_utilization_resource_observation')
    """)
        )
        .mappings()
        .one()
    )
    if row["n"] != 2 or row["measured_n"] != 2 or row["bytes"] is None:
        raise ValueError("unreliable utilization table/index byte statistics")
    return int(row["bytes"])


def _finalize_fallback(connection, state, week, now, reason):
    for resource_class, metrics in state["contracts"].items():
        for metric, entry in metrics.items():
            if entry.get("consumer_only"):
                continue
            predicate = and_(
                _account(weekly),
                weekly.c.week_start == week,
                weekly.c.resource_class == resource_class,
                weekly.c.metric == metric,
            )
            previous = (
                connection.execute(select(weekly).where(predicate).with_for_update())
                .mappings()
                .first()
            )
            values = (
                dict(previous["values_json"])
                if previous
                else {
                    "avg": None,
                    "peak": None,
                    "coverage": None,
                    "used_integral": None,
                    "capacity_integral": None,
                    "total_used_integral": None,
                }
            )
            values["quality"] = sorted(set(values.get("quality", [])) | {"finalization_failed"})
            values["finalization_error"] = reason[:256]
            item = dict(
                IDENTITY,
                week_start=week,
                resource_class=resource_class,
                metric=metric,
                contract_json=entry["contract"],
                values_json=values,
                status="final_incomplete"
                if previous and values.get("used_integral") is not None
                else "final_missing",
                quality_checkpoint_at=previous["quality_checkpoint_at"] if previous else now,
                finalized_at=now,
                updated_at=now,
            )
            if previous:
                connection.execute(weekly.update().where(predicate).values(**item))
            else:
                connection.execute(weekly.insert().values(**item))


def _calendar(connection, state, now, deadline):
    start = date.fromisoformat(state["collection_start_week"])
    stop = (
        date.fromisoformat(state["collection_stop_week"])
        if state.get("collection_stop_week")
        else policy.week_start(now)
    )
    closed = (
        date.fromisoformat(state["finalized_through_week"])
        if state.get("finalized_through_week")
        else start - timedelta(days=7)
    )
    week, processed = max(start, closed + timedelta(days=7)), 0
    while week <= stop and processed < 52 and time.monotonic() < deadline:
        if now < policy.day_start(week + timedelta(days=9)):
            break
        final = now >= policy.expires_at(week)
        try:
            _refresh_week(connection, state, week, now, final=final, checkpoint=True)
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            if not final:
                raise
            # Deterministic invalid facts must not hold detail forever at D.
            # Database/locking failures still abort: never purge without a committed result.
            _finalize_fallback(connection, state, week, now, str(exc))
        if final:
            state["finalized_through_week"] = week.isoformat()
        processed += 1
        week += timedelta(days=7)
    return processed


def maintain(
    engine, *, now=None, dry_run=False, batch_size=1000, max_rows=50000, max_seconds=300
) -> dict:
    if not 1 <= batch_size <= 1000 or not 0 <= max_rows <= 50_000 or not 0 < max_seconds <= 300:
        raise ValueError("maintenance budget exceeds approved bounds")
    if dry_run:
        with engine.connect() as connection:
            clock = _clock(connection, now)
            return {
                "dry_run": True,
                "state": _read_state(connection),
                "oldest_expired_at": _oldest_expired(connection, clock),
                "retained_rows": _count_rows(connection),
            }
    deadline, deleted, batches = time.monotonic() + max_seconds, 0, 0
    # Finalization and its watermark publish atomically, after the shared writer fence.
    with _transaction(engine) as connection:
        state = _read_state(connection, lock=True)
        clock = _clock(connection, now)
        if not state:
            return {"configured": False, "deleted_rows": 0}
        initial_health = policy.health(dict(state, paused=False), clock)
        if any(reason != "disabled_or_unconfigured" for reason in initial_health["reasons"]):
            state["paused"] = True
        weeks = _calendar(connection, state, clock, deadline)
        reconciled = state.get("reconciled_at")
        drift = False
        if (
            not reconciled
            or not state.get("counts_reliable")
            or clock - policy.utc_naive(reconciled) >= timedelta(days=1)
        ):
            actual = _count_rows(connection)
            drift = bool(state.get("counts_reliable") and actual != state["retained_rows"])
            # A capped reconciliation is a lower bound, never a reliable exact count.
            # Recalibrate every run while over cap so cleanup can continue safely.
            state.update(
                retained_rows=actual,
                counts_reliable=actual <= policy.ROW_LIMIT,
                reconciled_at=clock.isoformat(),
            )
        _save_state(connection, state)
    while deleted < max_rows and time.monotonic() < deadline:
        with _transaction(engine) as connection:
            state = _read_state(connection, lock=True)
            # Re-read DB time in each short transaction; never trust a pre-lock snapshot.
            clock = _clock(connection, now)
            oldest = _oldest_expired(connection, clock)
            if oldest is None:
                break
            # Oldest expired week globally; daily facts precede evidence in that week.
            candidate_weeks = [
                connection.execute(
                    select(table.c.week_start)
                    .where(_account(table), table.c.expires_at == oldest)
                    .order_by(table.c.id)
                    .limit(1)
                ).scalar()
                for table in (daily, observations)
            ]
            week = min(w for w in candidate_weeks if w is not None)
            closed = state.get("finalized_through_week")
            if not closed or week > date.fromisoformat(closed):
                # Unknown account/calendar data must never be deleted without finalization.
                break
            limit = min(batch_size, max_rows - deleted)
            table, ids = daily, []
            for candidate in (daily, observations):
                ids = list(
                    connection.execute(
                        select(candidate.c.id)
                        .where(
                            candidate.c.week_start == week,
                            candidate.c.expires_at <= clock,
                            _account(candidate),
                        )
                        .order_by(candidate.c.expires_at, candidate.c.id)
                        .limit(limit)
                    ).scalars()
                )
                table = candidate
                if ids:
                    break
            if not ids:
                break
            result = connection.execute(
                table.delete().where(
                    table.c.id.in_(ids),
                    _account(table),
                    table.c.week_start == week,
                    table.c.week_start <= date.fromisoformat(closed),
                    table.c.expires_at <= clock,
                )
            )
            removed = result.rowcount
            if removed != len(ids):
                raise ValueError("cleanup count unreliable")
            state["retained_rows"] -= removed
            if state["retained_rows"] < 0:
                raise ValueError("retained row counter drift")
            _save_state(connection, state)
        deleted += removed
        batches += 1
    with _transaction(engine) as connection:
        state = _read_state(connection, lock=True)
        clock = _clock(connection, now)
        oldest = _oldest_expired(connection, clock)
        try:
            size = _observed_bytes(connection)
            state.update(observed_bytes=size, bytes_observed_at=clock.isoformat())
        except (ValueError, TypeError):
            state.update(observed_bytes=None, bytes_observed_at=None)
        state.update(
            maintenance_at=clock.isoformat(),
            oldest_expired_at=oldest.isoformat() if oldest else None,
        )
        result = policy.health(dict(state, paused=False), clock)
        # Hysteresis: never resume while backlog or warning budgets remain.
        can_resume = (
            not oldest
            and state["retained_rows"] < policy.ROW_WARNING
            and state.get("observed_bytes") is not None
            and state["observed_bytes"] < policy.BYTE_WARNING
            and not drift
            and state["counts_reliable"]
        )
        hard = [reason for reason in result["reasons"] if reason != "disabled_or_unconfigured"]
        state["paused"] = bool(hard or drift or (state.get("paused") and not can_resume))
        state["pause_reasons"] = hard + (["count_drift"] if drift else [])
        if state["paused"] and not state["pause_reasons"]:
            state["pause_reasons"] = ["recovery_requires_healthy_below_warning_no_backlog"]
        state["warnings"] = result["warnings"]
        _save_state(connection, state)
    return {
        "configured": True,
        "deleted_rows": deleted,
        "committed_batches": batches,
        "weeks_processed": weeks,
        "state": state,
    }
