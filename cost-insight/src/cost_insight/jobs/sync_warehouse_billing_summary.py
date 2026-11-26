"""Bounded warehouse ingestion for ordinary AWS/Azure and explicitly scoped F04."""
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
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import get_cost_source
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.refresh_attribution_daily import (
    CostAttributionSource,
    _validate_tcms_allocation_table,
    run_refresh_cost_attribution_from_summary,
)
from cost_insight.jobs.sync_aws_billing_summary import AWS_TIDB_CLOUD_F04_SCHEMA_VERSION
from cost_insight.jobs.sync_azure_billing_summary import _normalize_summary_row as normalize_azure
from cost_insight.jobs.sync_gcp_billing_summary import (
    _normalize_summary_row as normalize_aws,
)
from cost_insight.jobs.sync_gcp_billing_summary import replace_summary_partition_usage_dates
from cost_insight.jobs.warehouse_recovery import assert_owner_stopped
from cost_insight.sources.aws_flat_cur_export import _AWS_TIDB_CLOUD_F04_USEDBY as F04_USEDBY
from cost_insight.sources.warehouse_billing_export import (
    F04_ACCOUNT_ID,
    WarehouseSnapshotError,
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


def _normalize(window: WarehouseWindow, vendor: str, account: str, *,
               f04: bool = False, allow_short_window: bool = False) -> dict:
    normalize = normalize_azure if vendor == "azure" else normalize_aws
    merged: dict[str, dict] = {}
    months = {window.start.replace(day=1), window.end.replace(day=1)}
    days = (window.end - window.start).days + 1
    if not 1 <= days <= 5 or (days != 5 and not allow_short_window):
        raise ValueError("Warehouse refresh requires a complete five-day window or bounded repair")
    for raw in window.rows:
        if f04:
            tags = json.loads(raw.get("vendor_tags_json") or "{}")
            if (raw.get("author") != F04_USEDBY or not isinstance(tags, dict)
                    or tags.get("usedby") != F04_USEDBY):
                raise ValueError("F04 source row is outside the required usedby boundary")
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
    f04 = (vendor == "aws" and account == F04_ACCOUNT_ID and source is not None
           and source.source_schema_version == AWS_TIDB_CLOUD_F04_SCHEMA_VERSION)
    expected_table = source_table(vendor, project)
    if (source is None or not source.is_active
            or (not f04 and (source.source_schema_version not in profiles
                            or (vendor == "aws" and account == F04_ACCOUNT_ID)))
            or source.source_table not in ((expected_table,) if f04 else (None, "", expected_table))):
        raise ValueError(f"Refusing inactive/custom/nonordinary warehouse source: {vendor}/{account}")
    if start and source.source_available_from and source.source_available_from > start:
        raise ValueError(f"Warehouse window predates source availability: {vendor}/{account}")
    return f04


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
    if _validate_registry(connection, vendor, account, project, window.start) != plan.get("f04", False):
        raise ValueError("Warehouse source profile changed since preflight")
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


def _locked_state(connection, name):
    # SQLite has no FOR UPDATE; obtain its writer lock before reading ownership.
    sql = ("UPDATE cost_job_state SET job_name=job_name WHERE job_name=:name"
           if connection.dialect.name == "sqlite" else
           "SELECT job_name FROM cost_job_state WHERE job_name=:name FOR UPDATE")
    connection.execute(text(sql), {"name": name})
    return state_store.get_job_state(connection, name)


def _resume_progress(previous, progress):
    old = previous.watermark
    for key in ("vendor", "project", "source_profile"):
        # Pre-F04 ordinary workers did not record a profile. The runner has
        # already rejected custom/F04 registry routes before constructing this scope.
        legacy_ordinary = key == "source_profile" and key not in old and progress[key] == "ordinary"
        if not legacy_ordinary and old.get(key) != progress[key]:
            raise ValueError(f"Previous warehouse worker is {previous.last_status}; inspect before recovery: {key}")
    accounts = old.get("accounts")
    if (not isinstance(accounts, (list, tuple)) or tuple(accounts) != progress["accounts"]
            or not isinstance(old.get("run_id"), str) or not old["run_id"]):
        raise ValueError("Warehouse recovery requires the original account scope and run identity; inspect before recovery")
    windows = old.get("windows")
    completed = old.get("completed_account_days")
    if not isinstance(windows, list) or not isinstance(completed, list):
        raise ValueError("Missing warehouse recovery windows/completions; inspect before recovery")  # noqa: TRY004
    expected, seen = set(), set()
    for window in windows:
        if not isinstance(window, dict) or not isinstance(window.get("account_id"), str):
            raise ValueError("Malformed warehouse recovery window; inspect before recovery")  # noqa: TRY004
        account = window["account_id"]
        try:
            start, end = _date(window.get("start")), _date(window.get("end"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Malformed warehouse recovery window; inspect before recovery") from exc
        if account not in progress["accounts"] or account in seen or not 0 <= (end - start).days <= 4:
            raise ValueError("Invalid warehouse recovery window; inspect before recovery")
        seen.add(account)
        expected.update(f"{account}/{day}" for day in _dates(start, end))
    current_day = old.get("current_account_day")
    if (not all(isinstance(day, str) for day in completed)
            or len(set(completed)) != len(completed) or not set(completed) <= expected
            or old.get("phase") not in ("preflight", "write", "complete")
            or (old["phase"] != "preflight" and seen != set(progress["accounts"]))
            or (current_day is not None and (not isinstance(current_day, str) or current_day not in expected))):
        raise ValueError("Incomplete or inconsistent warehouse recovery checkpoint; inspect before recovery")
    progress.update(windows=[dict(window) for window in windows], completed_account_days=list(completed),
                    recovered_from_run_id=old["run_id"])


def _acquire(engine, name, progress):
    with engine.connect() as connection:
        previous = state_store.get_job_state(connection, name)
    if previous and previous.last_status not in ("ready", "succeeded"):
        if previous.last_status not in ("failed", "running"):
            raise ValueError(f"Previous warehouse worker is {previous.last_status}; inspect before recovery")
        if previous.last_status == "running":
            # Never hold a database transaction across the Kubernetes request.
            try:
                assert_owner_stopped(previous.watermark)
            except ValueError as exc:
                raise ValueError(f"Previous warehouse worker is running; inspect before recovery: {exc}") from exc
        _resume_progress(previous, progress)
    with engine.begin() as connection:
        statement = "INSERT OR IGNORE" if connection.dialect.name == "sqlite" else "INSERT IGNORE"
        connection.execute(text(f"""{statement} INTO cost_job_state
            (job_name,watermark_json,last_status) VALUES (:name,'{{}}','ready')"""), {"name": name})
        current = _locked_state(connection, name)
        if ((previous is not None and current != previous)
                or (previous is None and current.last_status != "ready")):
            raise ValueError("Warehouse worker state changed during acquisition; retry next run")
        state_store.mark_job_started(connection, name, progress)


def _checkpoint(engine, name, progress, status="running", error=None):
    progress["last_update"] = datetime.now(UTC).isoformat()
    with engine.begin() as connection:
        state = _locked_state(connection, name)
        if state is None or state.last_status != "running" or state.watermark.get("run_id") != progress["run_id"]:
            raise ValueError("Lost warehouse worker state ownership")
        if status == "succeeded":
            state_store.mark_job_succeeded(connection, name, progress)
        elif status == "failed":
            state_store.mark_job_failed(connection, name, progress, error)
        else:
            state_store.checkpoint_job_watermark(connection, name, progress)
    _log("progress", progress=progress, status=status)


class WarehouseAccountFailures(ValueError):
    """All eligible accounts were attempted; expose their failures to the CLI."""

    def __init__(self, summary):
        self.summary = summary
        super().__init__("; ".join(f"{account}: {result['error']}" for account, result in
                                  summary["account_results"].items() if result.get("error")))


def _migrate_ordinary_aws_state(engine, project):
    """Fence old singleton workers atomically with preserving their scoped work."""
    name = "sync_aws_warehouse_daily"
    with engine.connect() as connection:
        previous = state_store.get_job_state(connection, name)
    if previous and previous.last_status == "account_scoped":
        return
    recovered = None
    if previous and previous.last_status not in ("ready", "succeeded"):
        if previous.last_status not in ("failed", "running"):
            raise ValueError(f"Previous warehouse worker is {previous.last_status}; inspect before recovery")
        if previous.last_status == "running":
            # Preserve PR954's proof/CAS recovery; never bypass a live old worker.
            try:
                assert_owner_stopped(previous.watermark)
            except ValueError as exc:
                raise ValueError(f"Previous warehouse worker is running; inspect before recovery: {exc}") from exc
        accounts = previous.watermark.get("accounts")
        if (not isinstance(accounts, (list, tuple)) or not accounts
                or any(not isinstance(a, str) or not a or a.strip() != a for a in accounts)
                or tuple(accounts) != tuple(sorted(set(accounts))) or F04_ACCOUNT_ID in accounts):
            raise ValueError("Ambiguous legacy warehouse account scope; inspect before recovery")
        recovered = {"vendor": "aws", "project": project, "source_profile": "ordinary", "accounts": tuple(accounts)}
        _resume_progress(previous, recovered)
        # A multi-account preflight/shared failure has no reliable account attribution.
        # Keep that legacy latch intact rather than silently declaring siblings safe.
        if len(accounts) > 1 and (previous.watermark.get("phase") != "write"
                                 or not previous.watermark.get("current_account_day")):
            raise ValueError("Unattributed legacy warehouse failure; inspect before recovery")
    with engine.begin() as connection:
        statement = "INSERT OR IGNORE" if connection.dialect.name == "sqlite" else "INSERT IGNORE"
        connection.execute(text(f"""{statement} INTO cost_job_state
            (job_name,watermark_json,last_status) VALUES (:name,'{{}}','ready')"""), {"name": name})
        current = _locked_state(connection, name)
        if ((previous is not None and current != previous)
                or (previous is None and current.last_status != "ready")):
            raise ValueError("Warehouse worker state changed during migration; retry next run")
        if recovered is not None:
            for account in recovered["accounts"]:
                scoped_name = source_job_name(name, vendor="aws", account_id=account)
                if state_store.get_job_state(connection, scoped_name) is not None:
                    raise ValueError("Legacy and account warehouse states overlap; inspect before recovery")
                scoped = {**previous.watermark, "accounts": [account],
                          "windows": [w for w in recovered["windows"] if w["account_id"] == account],
                          "completed_account_days": [day for day in recovered["completed_account_days"]
                                                     if day.rsplit("/", 1)[0] == account],
                          "legacy_job_name": name}
                if str(scoped.get("current_account_day", "")).rsplit("/", 1)[0] != account:
                    scoped.pop("current_account_day", None)
                state_store.mark_job_failed(connection, scoped_name, scoped,
                    f"Interrupted legacy checkpoint ({previous.watermark.get('current_account_day')}): {previous.last_error}")
        # Deliberately not ready/succeeded/failed/running: both old acquisition
        # implementations reject this permanent fence, including during rolling deploys.
        connection.execute(text("UPDATE cost_job_state SET last_status='account_scoped', "
                                "updated_at=CURRENT_TIMESTAMP WHERE job_name=:name"), {"name": name})


def run_sync_warehouse_billing_summary(
    engine: Engine, *, client, vendor: str, account_ids: Sequence[str], project: str,
    tcms_allocation_table: str, dry_run: bool = False, today: date | None = None,
    start_date: date | None = None, end_date: date | None = None,
) -> dict:
    """Validate eligible accounts together, then publish scoped days sequentially."""
    source_table(vendor, project)  # Validate the source route before acquiring state.
    if (not dry_run and engine.dialect.name != "sqlite"
            and not Path("/var/run/secrets/kubernetes.io/serviceaccount/token").is_file()):
        raise ValueError("Production warehouse writes must run inside GKE")
    accounts = tuple(sorted(account_ids))
    if not accounts or any(not a or a.strip() != a for a in accounts) or len(set(accounts)) != len(accounts):
        raise ValueError("Explicit, nonempty, unique warehouse account IDs are required")
    if (start_date is None) != (end_date is None):
        raise ValueError("start_date and end_date must be set together")
    resolved_today = today or datetime.now(UTC).date()
    if type(resolved_today) is not date:
        raise ValueError("today must be a UTC date")
    if start_date is not None and (type(start_date) is not date or type(end_date) is not date
            or not 0 <= (end_date - start_date).days <= 4 or end_date >= resolved_today):
        raise ValueError("Historical warehouse repair requires 1..5 inclusive days ending before today")
    _validate_tcms_allocation_table(engine, tcms_allocation_table)
    f04 = vendor == "aws" and F04_ACCOUNT_ID in accounts
    if f04 and len(accounts) != 1:
        raise ValueError("F04 must run separately; cannot mix with ordinary warehouse accounts")
    isolated = vendor == "aws" and not f04
    if not isolated:
        with engine.connect() as connection:
            profiles = [_validate_registry(connection, vendor, account, project) for account in accounts]
        if any(profiles) != f04:
            raise ValueError("Warehouse source profile changed since preflight")
    if start_date is not None and not f04:
        raise ValueError("Explicit historical warehouse windows are currently restricted to F04")
    # F04 and Azure keep their existing scope and recovery keys.
    name = "sync_aws_f04_warehouse_daily" if f04 else f"sync_{vendor}_warehouse_daily"
    progress = {"run_id": str(uuid.uuid4()), "pod_name": os.getenv("POD_NAME"),
                "pod_uid": os.getenv("POD_UID"), "pod_namespace": os.getenv("POD_NAMESPACE"),
                "vendor": vendor, "project": project,
                "source_profile": AWS_TIDB_CLOUD_F04_SCHEMA_VERSION if f04 else "ordinary",
                "accounts": accounts,
                "windows": ([{"account_id": account, "start": str(start_date), "end": str(end_date)}
                             for account in accounts] if start_date is not None else []),
                "completed_account_days": [],
                "percent": 0, "phase": "preflight", "dry_run": dry_run}
    if isolated and not dry_run:
        _migrate_ordinary_aws_state(engine, project)
    scopes = ({account: {"name": source_job_name(name, vendor=vendor, account_id=account),
                         "progress": {**progress, "run_id": str(uuid.uuid4()), "accounts": (account,),
                                      "windows": [], "completed_account_days": []}}
               for account in accounts} if isolated else {"shared": {"name": name, "progress": progress}})

    def scope_for(account):
        return scopes[account if isolated else "shared"]

    def fail(scope, exc):
        scope["error"] = exc
        if scope.get("owned"):
            try:
                # Ownership/status checks also protect a succeeded commit whose ACK was lost.
                _checkpoint(engine, scope["name"], scope["progress"], "failed", repr(exc))
            except Exception:  # noqa: BLE001 - bookkeeping must not mask the original failure
                LOG.warning("Could not persist warehouse failure state; preserving original error")
            scope["owned"] = False
        _log("account_failed", accounts=scope["progress"]["accounts"], error=repr(exc))

    def heartbeat(scope):
        # Only this read's scope gets checkpointed.
        # Other acquired accounts remain protected by the same live Pod owner.
        if scope.get("owned") and time.monotonic() - scope.get("last_heartbeat", 0) >= 60:
            _checkpoint(engine, scope["name"], scope["progress"])
            scope["last_heartbeat"] = time.monotonic()

    try:
        for scope in scopes.values():
            try:
                if not dry_run:
                    _acquire(engine, scope["name"], scope["progress"])
                    scope["owned"] = True
            except Exception as exc:
                if not isolated:
                    raise
                fail(scope, exc)
        plans, snapshots = [], {}
        for account in accounts:
            scope = scope_for(account)
            if "error" in scope:
                continue
            name, progress = scope["name"], scope["progress"]
            try:
                with engine.connect() as connection:
                    if _validate_registry(connection, vendor, account, project) != f04:
                        raise ValueError("Warehouse source profile changed since preflight")
                saved = next((w for w in progress["windows"] if w["account_id"] == account), None)
                expected = (_date(saved["start"]), _date(saved["end"])) if saved else None
                if expected and start_date is not None and expected != (start_date, end_date):
                    raise ValueError("Explicit warehouse dates differ from the recovery checkpoint")
                bounds = ({"recovery_window": expected} if expected else
                          {"start_date": start_date, "end_date": end_date} if start_date is not None else {})
                window = fetch_warehouse_window(client, vendor=vendor, account_id=account, project=project,
                                                 today=resolved_today, heartbeat=partial(heartbeat, scope),
                                                 **({"f04": True} if f04 else {}), **bounds)
                requested_window = expected if expected is not None else (
                    (start_date, end_date) if start_date is not None else None)
                if requested_window is not None and (window.start, window.end) != requested_window:
                    raise ValueError("Historical warehouse source returned a different date window")
                if (type(window.start) is not date or type(window.end) is not date
                        or not 0 <= (window.end - window.start).days <= 4 or window.end >= resolved_today):
                    raise ValueError("Invalid resolved warehouse window")
                if not saved:
                    progress["windows"].append({"account_id": account, "start": str(window.start), "end": str(window.end)})
                    if not dry_run:
                        _checkpoint(engine, name, progress)
                plan = {"account": account, "window": window, "f04": f04,
                        "groups": _normalize(window, vendor, account, f04=f04,
                                             allow_short_window=expected is not None or start_date is not None)}
                with engine.connect() as connection:
                    _baseline(connection, plan, vendor, project)
                    for day in _dates(window.start, window.end):
                        if f"{account}/{day}" in progress["completed_account_days"]:
                            _verify_publication(connection, vendor, account, day)
            except WarehouseSnapshotError:
                raise
            except Exception as exc:
                if not isolated:
                    raise
                fail(scope, exc)
                continue
            # Unlike account-local validation failures, changed shared snapshots
            # invalidate ALL prefetched plans, including before the first write.
            for table, metadata in window.snapshots.items():
                if table in snapshots and snapshots[table] != metadata:
                    raise WarehouseSnapshotError("Warehouse snapshot changed between account reads")
                snapshots[table] = metadata
            plans.append(plan)
            _log("window_validated", vendor=vendor, account_id=account,
                 start=str(window.start), end=str(window.end), controls=window.controls)
        if plans:
            assert_snapshot(client, snapshots)
        if not dry_run:
            for plan in plans:
                account, window = plan["account"], plan["window"]
                scope = scope_for(account)
                name, progress = scope["name"], scope["progress"]
                total_account_days = sum((p["window"].end - p["window"].start).days + 1 for p in plans
                                         if not isolated or p["account"] == account)
                try:
                    progress["percent"] = 100 * len(progress["completed_account_days"]) // total_account_days
                    progress["phase"] = "write"
                    _checkpoint(engine, name, progress)
                    for day in _dates(window.start, window.end):
                        account_day = f"{account}/{day}"
                        if account_day in progress["completed_account_days"]:
                            continue
                        progress["current_account_day"] = account_day
                        _checkpoint(engine, name, progress)
                        with engine.connect() as connection:
                            if _validate_registry(connection, vendor, account, project, window.start) != f04:
                                raise ValueError("Warehouse source profile changed since preflight")
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
                            _verify_publication(connection, vendor, account, day)
                        progress["completed_account_days"].append(progress["current_account_day"])
                        progress["percent"] = 100 * len(progress["completed_account_days"]) // total_account_days
                        _checkpoint(engine, name, progress)
                    if isolated:
                        progress["phase"] = "complete"
                        _checkpoint(engine, name, progress, "succeeded")
                        scope["owned"] = False
                except WarehouseSnapshotError:
                    raise
                except Exception as exc:
                    if not isolated:
                        raise
                    fail(scope, exc)
            if not isolated:
                progress["phase"] = "complete"
                _checkpoint(engine, name, progress, "succeeded")
                scopes["shared"]["owned"] = False
    except BaseException as exc:
        for scope in scopes.values():
            if scope.get("owned") or (dry_run and "error" not in scope):
                fail(scope, exc)
        # Multi-account failures, including shared aborts, have one JSON/CLI exit.
        # Cancellation still cleans ownership and propagates, as do legacy scopes.
        if not isolated or len(accounts) == 1 or not isinstance(exc, Exception):
            raise
    errors = [scope["error"] for scope in scopes.values() if "error" in scope]
    if not isolated or len(accounts) == 1:
        if errors:
            raise errors[0]
        return scope_for(accounts[0])["progress"]
    summary = {"vendor": vendor, "dry_run": dry_run, "account_results": {
        account: {"job_name": scope["name"],
                  "status": "failed" if "error" in scope else "dry_run" if dry_run else "succeeded",
                  "error": repr(scope["error"]) if "error" in scope else None,
                  "progress": scope["progress"]} for account, scope in scopes.items()}}
    if errors:
        raise WarehouseAccountFailures(summary)
    return summary
