"""Bounded warehouse ingestion shared by ordinary AWS and Azure daily jobs."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import get_cost_source
from cost_insight.jobs.refresh_attribution_daily import (
    CostAttributionSource,
    _validate_tcms_allocation_table,
    run_refresh_cost_attribution_from_summary,
)
from cost_insight.jobs.sync_azure_billing_summary import _normalize_summary_row as normalize_azure
from cost_insight.jobs.sync_gcp_billing_summary import (
    _normalize_summary_row as normalize_aws,
)
from cost_insight.jobs.sync_gcp_billing_summary import replace_summary_partition_usage_dates
from cost_insight.sources.warehouse_billing_export import (
    WarehouseWindow,
    assert_snapshot,
    fetch_warehouse_window,
    source_table,
)

LOG = logging.getLogger(__name__)
METRICS = ("list_cost", "effective_cost", "credit_amount", "net_cost")
SUMMARY = "cost_bq_export_summary_daily"
TOLERANCE = Decimal("0.00001")


def _log(event: str, **fields) -> None:
    # The application formatter does not render logging.extra fields.
    LOG.info("%s", json.dumps({"event": event, **fields}, default=str, sort_keys=True))


def _amount(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Null or invalid warehouse monetary value") from exc
    if not result.is_finite():
        raise ValueError("Non-finite warehouse monetary value")
    return result


def _totals(rows) -> dict[str, Decimal]:
    return {key: sum((_amount(row[key]) for row in rows), Decimal(0)) for key in METRICS}


def _assert_money(expected, actual, label: str) -> None:
    for key in METRICS:
        if abs(_amount(expected[key]) - _amount(actual[key])) > TOLERANCE:
            raise ValueError(f"{label}: {key}: {expected[key]} != {actual[key]}")


def _dates(start: date, end: date):
    while start <= end:
        yield start
        start += timedelta(days=1)


def _date(value) -> date:
    if isinstance(value, datetime):
        raise TypeError("Expected a usage date, not a timestamp")
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _normalize(window: WarehouseWindow, vendor: str, account: str) -> dict:
    normalize = normalize_azure if vendor == "azure" else normalize_aws
    merged: dict[str, dict] = {}
    months = {window.start.replace(day=1), window.end.replace(day=1)}
    if (window.end - window.start).days != 4:
        raise ValueError("Warehouse refresh requires a complete five-day window")
    for raw in window.rows:
        if ((raw.get("vendor"), raw.get("account_id"), raw.get("currency")) != (vendor, account, "USD")
                or raw.get("source_schema_version")
                or raw.get("source_allocation_scope", "direct") != "direct"):
            raise ValueError("Unexpected warehouse identity, currency or profile")
        _totals([raw])
        row = normalize(raw)
        if (not window.start <= row["usage_date"] <= window.end
                or row["export_partition_date"] not in months):
            raise ValueError("Warehouse row is outside the requested billing-partition/day scope")
        key = row["source_row_hash"]
        row.update({metric: _amount(row[metric]) for metric in METRICS})
        if key not in merged:
            merged[key] = row
        else:
            previous = merged[key]
            for metric in METRICS:
                previous[metric] += row[metric]
            timestamps = [r["source_export_time"] for r in (previous, row) if r.get("source_export_time")]
            previous["source_export_time"] = max(timestamps) if timestamps else None
    groups = defaultdict(list)
    for row in merged.values():
        groups[row["export_partition_date"], row["usage_date"]].append(row)
    controls = {}
    for control in window.controls:
        key = (_date(control["export_partition_date"]), _date(control["usage_date"]))
        if (key in controls or key[0] not in months or not window.start <= key[1] <= window.end
                or int(control["n"]) <= 0 or int(control["non_usd"]) != 0
                or int(control.get("invalid_money", 0)) != 0):
            raise ValueError("Invalid raw warehouse control or currency")
        controls[key] = control
    if set(controls) != set(groups):
        raise ValueError("Raw and normalized billing-partition/day coverage differs")
    for key, rows in groups.items():
        if vendor == "azure":
            _assert_money(controls[key], _totals(rows), "raw -> normalized Azure")
        elif abs(_amount(controls[key]["list_cost"]) - _totals(rows)["list_cost"]) > TOLERANCE:
            # AWS effective/net amounts are rounded per SQL group, not per raw day.
            raise ValueError("Raw and normalized AWS list cost differs")
    if {key[1] for key in groups} != set(_dates(window.start, window.end)):
        raise ValueError("Empty source day is not an authoritative zero")
    return dict(groups)


def _validate_registry(connection: Connection, vendor: str, account: str, project: str, start=None):
    source = get_cost_source(connection, vendor=vendor, account_id=account)
    profiles = (None, "", "aws_cur_legacy_v1") if vendor == "aws" else (None, "")
    if (source is None or not source.is_active or source.source_schema_version not in profiles
            or source.source_table not in (None, "", source_table(vendor, project))):
        raise ValueError(f"Refusing inactive/custom/nonordinary warehouse source: {vendor}/{account}")
    if start and source.source_available_from and source.source_available_from > start:
        raise ValueError(f"Warehouse window predates source availability: {vendor}/{account}")


def _table_totals(connection, table, vendor, account, day, *, partition=None, outside=None, version=None):
    if table not in (SUMMARY, "cost_attribution_daily", "cost_resource_serving_daily"):
        raise ValueError("Unsupported warehouse verification table")
    params = {"vendor": vendor, "account": account, "day": day}
    clause = ""
    if partition is not None:
        clause += " AND export_partition_date=:partition"
        params["partition"] = partition
    if outside is not None:
        clause += " AND (export_partition_date < :start OR export_partition_date > :end OR export_partition_date IS NULL)"
        params.update(start=outside[0], end=outside[1])
    if version is not None:
        clause += " AND basis_key='native' AND materialization_version=:version"
        params["version"] = version
    sums = ",".join(f"COALESCE(SUM({m}),0) AS {m}" for m in METRICS)
    nulls = " OR ".join(f"{m} IS NULL" for m in METRICS)
    rows = connection.execute(text(f"""SELECT currency,COUNT(*) AS n,{sums},
        SUM(CASE WHEN {nulls} THEN 1 ELSE 0 END) AS invalid_money FROM {table}
        WHERE vendor=:vendor AND account_id=:account AND usage_date=:day {clause}
        GROUP BY currency"""), params).mappings().all()
    if not rows:
        return {"currency": "USD", "n": 0, **_totals([])}
    if len(rows) != 1 or rows[0]["currency"] != "USD" or rows[0]["invalid_money"]:
        raise ValueError(f"Mixed currency or null money in {table}")
    return {**rows[0], **_totals(rows)}


def _baseline(connection, plan, vendor, project):
    account, window = plan["account"], plan["window"]
    _validate_registry(connection, vendor, account, project, window.start)
    latest = connection.execute(text(f"""SELECT MAX(usage_date) FROM {SUMMARY}
        WHERE vendor=:vendor AND account_id=:account AND usage_date <= :end"""),
        {"vendor": vendor, "account": account, "end": window.end}).scalar_one()
    if latest is None or _date(latest) < window.start - timedelta(days=1):
        raise ValueError(f"Local backlog exceeds the five-day window: {vendor}/{account}")
    bounds = (window.start.replace(day=1), window.end.replace(day=1))
    plan["before"], plan["protected"] = {}, {}
    for day in _dates(window.start, window.end):
        plan["protected"][day] = _table_totals(connection, SUMMARY, vendor, account, day, outside=bounds)
        for month in set(bounds):
            key = month, day
            current = _table_totals(connection, SUMMARY, vendor, account, day, partition=month)
            if current["n"] and not plan["groups"].get(key):
                raise ValueError(f"Empty queried partition/day has local rows: {vendor}/{account}/{key}")
            plan["before"][key] = current


def _verify_scopes(connection, plan, vendor, day, *, before=False):
    account, window = plan["account"], plan["window"]
    bounds = (window.start.replace(day=1), window.end.replace(day=1))
    for month in set(bounds):
        actual = _table_totals(connection, SUMMARY, vendor, account, day, partition=month)
        expected = plan["before"][month, day] if before else {
            "n": len(plan["groups"].get((month, day), [])),
            **_totals(plan["groups"].get((month, day), [])),
        }
        if actual["n"] != expected["n"]:
            raise ValueError("Warehouse summary identity count changed or differs from source")
        _assert_money(expected, actual, "preflight -> summary" if before else "source -> summary")
    protected = _table_totals(connection, SUMMARY, vendor, account, day, outside=bounds)
    if protected["n"] != plan["protected"][day]["n"]:
        raise ValueError("Unqueried billing-partition count changed")
    _assert_money(plan["protected"][day], protected, "unqueried billing partitions")


def _verify_publication(connection, vendor, account, day):
    summary = _table_totals(connection, SUMMARY, vendor, account, day)
    attr = _table_totals(connection, "cost_attribution_daily", vendor, account, day)
    if not summary["n"] or not attr["n"]:
        raise ValueError("Missing warehouse summary or attribution")
    _assert_money(summary, attr, "summary -> attribution")
    pointer = connection.execute(text("""SELECT * FROM cost_resource_serving_publication
        WHERE basis_key='native' AND vendor=:vendor AND account_id=:account AND usage_date=:day"""),
        {"vendor": vendor, "account": account, "day": day}).mappings().one()
    if pointer["currency"] != "USD" or not pointer["active_materialization_version"]:
        raise ValueError("Missing native publication version/currency")
    serving = _table_totals(connection, "cost_resource_serving_daily", vendor, account, day,
                            version=pointer["active_materialization_version"])
    if not serving["n"]:
        raise ValueError("Published native version is empty")
    _assert_money(attr, serving, "attribution -> published native serving")
    if abs(_amount(pointer["total_list_cost"]) - summary["list_cost"]) > TOLERANCE:
        raise ValueError("Native publication pointer total differs")
    _log("day_verified", vendor=vendor, account_id=account, usage_date=str(day),
         summary=summary, attribution=attr, serving=serving)


def _acquire(engine, name, progress):
    with engine.begin() as connection:
        sqlite = connection.dialect.name == "sqlite"
        statement = "INSERT OR IGNORE" if sqlite else "INSERT IGNORE"
        connection.execute(text(f"""{statement} INTO cost_job_state
            (job_name,watermark_json,last_status) VALUES (:name,'{{}}','ready')"""), {"name": name})
        status = connection.execute(text("SELECT last_status FROM cost_job_state WHERE job_name=:name" +
                                   ("" if sqlite else " FOR UPDATE")), {"name": name}).scalar_one()
        if status not in ("ready", "succeeded"):
            raise ValueError(f"Previous warehouse worker is {status}; inspect before recovery")
        state_store.mark_job_started(connection, name, progress)


def _checkpoint(engine, name, progress, status="running", error=None):
    progress["last_update"] = datetime.now(UTC).isoformat()
    with engine.begin() as connection:
        state = state_store.get_job_state(connection, name)
        if state is None or state.last_status != "running" or state.watermark.get("run_id") != progress["run_id"]:
            raise ValueError("Lost warehouse worker state ownership")
        if status == "succeeded":
            state_store.mark_job_succeeded(connection, name, progress)
        elif status == "failed":
            state_store.mark_job_failed(connection, name, progress, error)
        else:
            state_store.checkpoint_job_watermark(connection, name, progress)
    _log("progress", progress=progress, status=status)


def run_sync_warehouse_billing_summary(
    engine: Engine, *, client, vendor: str, account_ids: Sequence[str], project: str,
    tcms_allocation_table: str, dry_run: bool = False, today: date | None = None,
) -> dict:
    """Fetch all accounts first, then replace/attribute/publish one scoped day at a time."""
    source_table(vendor, project)  # Validate the source route before acquiring state.
    if (not dry_run and engine.dialect.name != "sqlite"
            and not Path("/var/run/secrets/kubernetes.io/serviceaccount/token").is_file()):
        raise ValueError("Production warehouse writes must run inside GKE")
    accounts = tuple(sorted(account_ids))
    if not accounts or any(not a or a.strip() != a for a in accounts) or len(set(accounts)) != len(accounts):
        raise ValueError("Explicit, nonempty, unique warehouse account IDs are required")
    _validate_tcms_allocation_table(engine, tcms_allocation_table)
    # Keep the operational worker's keys so a failed/running migration is never bypassed.
    name = f"sync_{vendor}_warehouse_daily"
    progress = {"run_id": str(uuid.uuid4()), "pod_name": os.getenv("POD_NAME"), "vendor": vendor, "project": project,
                "accounts": accounts, "windows": [], "completed_account_days": [],
                "percent": 0, "phase": "preflight", "dry_run": dry_run}
    if not dry_run:
        _acquire(engine, name, progress)
    last_heartbeat = 0.0

    def heartbeat():
        nonlocal last_heartbeat
        if not dry_run and time.monotonic() - last_heartbeat >= 60:
            _checkpoint(engine, name, progress)
            last_heartbeat = time.monotonic()

    try:
        plans, snapshots = [], {}
        for account in accounts:
            with engine.connect() as connection:
                _validate_registry(connection, vendor, account, project)
            window = fetch_warehouse_window(client, vendor=vendor, account_id=account, project=project,
                                             today=today or datetime.now(UTC).date(), heartbeat=heartbeat)
            progress["windows"].append({"account_id": account, "start": str(window.start), "end": str(window.end)})
            plan = {"account": account, "window": window, "groups": _normalize(window, vendor, account)}
            with engine.connect() as connection:
                _baseline(connection, plan, vendor, project)
            for table, metadata in window.snapshots.items():
                if table in snapshots and snapshots[table] != metadata:
                    raise ValueError("Warehouse snapshot changed between account reads")
                snapshots[table] = metadata
            plans.append(plan)
            _log("window_validated", vendor=vendor, account_id=account,
                 start=str(window.start), end=str(window.end), controls=window.controls)
        assert_snapshot(client, snapshots)
        if dry_run:
            return progress
        progress["phase"] = "write"
        _checkpoint(engine, name, progress)
        for plan in plans:
            account, window = plan["account"], plan["window"]
            for day in _dates(window.start, window.end):
                progress["current_account_day"] = f"{account}/{day}"
                _checkpoint(engine, name, progress)
                assert_snapshot(client, snapshots)
                with engine.connect() as connection:
                    _validate_registry(connection, vendor, account, project, window.start)
                    _verify_scopes(connection, plan, vendor, day, before=True)
                for (month, usage_day), rows in sorted(plan["groups"].items()):
                    if usage_day != day:
                        continue
                    assert_snapshot(client, snapshots)
                    written = replace_summary_partition_usage_dates(
                        engine, rows, vendor=vendor, account_id=account, export_partition_date=month,
                        usage_start_date=day, usage_end_date=day, dry_run=False, batch_size=500)
                    _log("summary_scope_replaced", vendor=vendor, account_id=account,
                         usage_date=str(day), billing_partition=str(month),
                         rows_deleted=plan["before"][month, day]["n"], rows_inserted=written)
                with engine.connect() as connection:
                    _verify_scopes(connection, plan, vendor, day)
                run_refresh_cost_attribution_from_summary(
                    engine, source=CostAttributionSource(vendor, account), start_date=day, end_date=day,
                    tcms_allocation_table=tcms_allocation_table)
                with engine.connect() as connection:
                    _verify_scopes(connection, plan, vendor, day)
                    _verify_publication(connection, vendor, account, day)
                progress["completed_account_days"].append(progress["current_account_day"])
                progress["percent"] = 100 * len(progress["completed_account_days"]) // (5 * len(plans))
                _checkpoint(engine, name, progress)
        progress["phase"] = "complete"
        _checkpoint(engine, name, progress, "succeeded")
        return progress
    except BaseException as exc:
        if not dry_run:
            _checkpoint(engine, name, progress, "failed", repr(exc))
        raise
