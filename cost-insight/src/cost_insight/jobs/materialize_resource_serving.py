"""Build the bounded, published resource drilldown projection.

The raw resource ledger is deliberately not consulted by the Dashboard.  This
job resolves its exact summary lineage once, writes a private version, checks
conservation, and then moves a small daily publication pointer.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.config import TENCENT_CI_SOURCE
from cost_insight.common.row_utils import bind_decimal_rows, tencent_ci_pool_key

_AMOUNT_QUANTUM = Decimal("0.000000001")
_AMOUNTS = ("list_cost", "effective_cost", "credit_amount", "net_cost")
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaterializeResourceServingSummary:
    start_date: date
    end_date: date
    materialization_version: str
    bases: tuple[str, ...]
    windows_published: int
    rows_written: int
    dry_run: bool


def run_materialize_resource_serving(
    engine: Engine,
    *,
    start_date: date,
    end_date: date,
    basis: str | None = None,
    processing_start_date: date | None = None,
    processing_end_date: date | None = None,
    materialization_version: str | None = None,
    vendor: str | None = None,
    account_id: str | None = None,
    dry_run: bool = False,
    batch_size: int = 1_000,
    now: datetime | None = None,
) -> MaterializeResourceServingSummary:
    """Stage and publish each daily source window independently.

    Resource serving uses native attribution. Tencent CI additionally projects it
    onto summary resource lineage.
    """
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")
    processing_start = processing_start_date or start_date
    processing_end = processing_end_date or end_date
    if not (start_date <= processing_start <= processing_end <= end_date):
        raise ValueError("processing dates must be within the requested range")
    if basis not in (None, "native"):
        raise ValueError(f"unsupported resource serving basis: {basis!r}")
    if (vendor is None) != (account_id is None):
        raise ValueError("vendor and account_id must be supplied together")

    version = materialization_version or (now or datetime.now(UTC)).strftime(
        "resource_%Y%m%dT%H%M%S%f"
    )
    bases = ("native",)
    if not _serving_schema_ready(engine):
        LOG.warning("resource serving materialization skipped because required serving migrations are missing")
        return MaterializeResourceServingSummary(
            start_date=start_date,
            end_date=end_date,
            materialization_version=version,
            bases=bases,
            windows_published=0,
            rows_written=0,
            dry_run=dry_run,
        )

    windows_published = 0
    rows_written = 0
    for basis_key in bases:
        with engine.begin() as connection:
            windows = _source_windows(
                connection,
                start_date=processing_start,
                end_date=processing_end,
                vendor=vendor,
                account_id=account_id,
            )
        for window in windows:
            params = dict(window)
            is_tencent_ci = (params["vendor"], params["account_id"]) == TENCENT_CI_SOURCE
            with engine.begin() as connection:
                source_rows = _load_source_rows(connection, **params)
                detail_rows = () if is_tencent_ci else _load_detail_rows(connection, **params)
                tencent_summary_rows = (
                    _load_tencent_summary_rows(connection, **params) if is_tencent_ci else ()
                )
            builder = (
                build_tencent_resource_serving_rows
                if is_tencent_ci
                else build_resource_serving_rows
            )
            serving_rows = builder(
                source_rows=source_rows,
                **(
                    {"summary_rows": tencent_summary_rows}
                    if is_tencent_ci
                    else {"detail_rows": detail_rows}
                ),
                basis_key=basis_key,
                materialization_version=version,
                calculated_at=(now or datetime.now(UTC)).replace(tzinfo=None),
            )
            _assert_conserved(source_rows, serving_rows)
            if not dry_run:
                _replace_staged_window(
                    engine,
                    serving_rows,
                    materialization_version=version,
                    basis_key=basis_key,
                    batch_size=batch_size,
                    **params,
                )
                _publish_window(
                    engine,
                    basis_key=basis_key,
                    materialization_version=version,
                    rows=serving_rows,
                    source_rows=source_rows,
                    **params,
                )
            windows_published += 0 if dry_run else 1
            rows_written += 0 if dry_run else len(serving_rows)

    return MaterializeResourceServingSummary(
        start_date=start_date,
        end_date=end_date,
        materialization_version=version,
        bases=tuple(bases),
        windows_published=windows_published,
        rows_written=rows_written,
        dry_run=dry_run,
    )


def build_resource_serving_rows(
    *,
    source_rows: Iterable[Mapping[str, Any]],
    detail_rows: Iterable[Mapping[str, Any]],
    basis_key: str,
    materialization_version: str,
    calculated_at: datetime,
) -> tuple[dict[str, Any], ...]:
    """Resolve direct/grouped exact lineage and aggregate a serving window."""
    details_by_summary: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for detail in detail_rows:
        source_hash = str(detail.get("source_summary_row_hash") or "")
        if source_hash:
            details_by_summary[source_hash].append(detail)

    contributions: list[dict[str, Any]] = []
    for source in source_rows:
        for resolved in (dict(source),):
            source_hash = str(resolved.get("source_summary_row_hash") or "")
            details = details_by_summary.get(source_hash, ()) if source_hash else ()
            contributions.extend(
                _detail_or_fallback_contributions(
                    source=resolved,
                    details=details,
                    basis_key=basis_key,
                    materialization_version=materialization_version,
                    calculated_at=calculated_at,
                )
            )

    return _aggregate_contributions(contributions)


def build_tencent_resource_serving_rows(
    *,
    source_rows: Iterable[Mapping[str, Any]],
    summary_rows: Iterable[Mapping[str, Any]],
    basis_key: str,
    materialization_version: str,
    calculated_at: datetime,
) -> tuple[dict[str, Any], ...]:
    """Project Tencent build-weighted attribution back onto provider resources."""
    sources = tuple(dict(row) for row in source_rows)
    summaries = tuple(dict(row) for row in summary_rows)
    summaries_by_hash = {str(row["source_row_hash"]): row for row in summaries}
    direct_hashes = {
        str(row["source_summary_row_hash"])
        for row in sources
        if row.get("source_summary_row_hash")
    }
    missing_hashes = direct_hashes - summaries_by_hash.keys()
    if missing_hashes:
        raise RuntimeError(f"Tencent source-summary hash is missing: {sorted(missing_hashes)[0]}")
    contributions = [
        _tencent_resource_contribution(
            source,
            summaries_by_hash[str(source["source_summary_row_hash"])],
            {name: _decimal_or_none(source.get(name)) for name in _AMOUNTS},
            basis_key=basis_key,
            materialization_version=materialization_version,
            calculated_at=calculated_at,
        )
        for source in sources
        if source.get("source_summary_row_hash")
    ]

    shared_sources: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        if source.get("source_allocation_scope") == "tencent_ci_shared":
            shared_sources[tencent_ci_pool_key(source)].append(source)

    resources: dict[tuple[str, str, str, str], dict[tuple[str, str, str], dict[str, Any]]] = defaultdict(dict)
    for summary in summaries:
        if str(summary["source_row_hash"]) in direct_hashes:
            continue
        pool_resources = resources[tencent_ci_pool_key(summary)]
        resource_name = str(summary.get("resource_name") or "")
        resource_key = (
            resource_name,
            str(summary.get("vendor_tags_json") or ""),
            "" if resource_name else str(summary["source_row_hash"]),
        )
        resource = pool_resources.setdefault(
            resource_key,
            {
                **summary,
                "source_rows": 0,
                **{name: None for name in _AMOUNTS},
            },
        )
        resource["source_rows"] += 1
        for name in _AMOUNTS:
            value = _decimal_or_none(summary.get(name))
            if value is not None:
                resource[name] = _decimal(resource.get(name)) + value

    for pool_key, pool_sources in shared_sources.items():
        pool_resources = tuple(resources.get(pool_key, {}).values())
        if not pool_resources:
            raise RuntimeError(f"Tencent shared attribution has no source resources: {pool_key!r}")
        reference_field = _tencent_reference_field(pool_sources)
        reference_amounts = {
            _tencent_participant_key(row): _decimal(row.get(reference_field))
            for row in pool_sources
        }
        participants = sorted(pool_sources, key=_tencent_participant_sort_key)
        reference_total = sum(
            (_decimal(row.get(reference_field)) for row in pool_sources), Decimal()
        )
        for resource in pool_resources:
            remaining = {name: _decimal_or_none(resource.get(name)) for name in _AMOUNTS}
            for index, source in enumerate(participants):
                last = index == len(participants) - 1
                amounts: dict[str, Decimal | None] = {}
                for name, amount in remaining.items():
                    if amount is None:
                        amounts[name] = None
                    elif last:
                        amounts[name] = amount
                    elif reference_total == 0:
                        amounts[name] = Decimal()
                    else:
                        amounts[name] = (
                            _decimal(resource.get(name))
                            * reference_amounts[_tencent_participant_key(source)]
                            / reference_total
                        ).quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_UP)
                        remaining[name] -= amounts[name]
                contributions.append(
                    _tencent_resource_contribution(
                        source,
                        resource,
                        amounts,
                        basis_key=basis_key,
                        materialization_version=materialization_version,
                        calculated_at=calculated_at,
                    )
                )

    return _aggregate_contributions(contributions)



def _tencent_reference_field(rows: Sequence[Mapping[str, Any]]) -> str:
    return max(
        _AMOUNTS,
        key=lambda name: abs(sum((_decimal(row.get(name)) for row in rows), Decimal())),
    )


def _tencent_participant_key(row: Mapping[str, Any]) -> tuple[int | None, str, str]:
    employee_id = row.get("employee_id")
    return (
        int(employee_id) if employee_id is not None else None,
        str(row.get("org") or ""),
        str(row.get("repo") or ""),
    )


def _tencent_participant_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    employee_id, org, repo = _tencent_participant_key(row)
    return employee_id is None, employee_id or 0, org, repo


def _tencent_resource_contribution(
    source: Mapping[str, Any],
    resource: Mapping[str, Any],
    amounts: Mapping[str, Decimal | None],
    *,
    basis_key: str,
    materialization_version: str,
    calculated_at: datetime,
) -> dict[str, Any]:
    resource_id = str(resource.get("resource_name") or "") or None
    scoped_source = {**source, "source_rows": int(resource.get("source_rows") or source.get("source_rows") or 1)}
    if resource_id is None:
        row = _base_serving_row(
            {
                **scoped_source,
                "source_fact_hash": resource.get("source_row_hash") or source.get("source_fact_hash"),
                "resource_name": resource.get("resource_name"),
                "service_name": resource.get("service_name") or source.get("service_name"),
                "vendor_tags_json": resource.get("vendor_tags_json") or source.get("vendor_tags_json"),
            },
            detail=None,
            identity_kind="attribution_fallback",
            basis_key=basis_key,
            materialization_version=materialization_version,
            calculated_at=calculated_at,
        )
        row.update(amounts)
        row["detail_list_cost"] = Decimal()
        row["fallback_list_cost"] = _decimal(amounts.get("list_cost"))
        return row

    row = _base_serving_row(
        scoped_source,
        detail={
            "resource_id": resource_id,
            "resource_name": resource_id,
            "parent_resource_name": None,
            "service_name": resource.get("service_name"),
            "vendor_tags_json": resource.get("vendor_tags_json"),
        },
        identity_kind="resource_detail",
        basis_key=basis_key,
        materialization_version=materialization_version,
        calculated_at=calculated_at,
    )
    row.update(amounts)
    row["detail_list_cost"] = _decimal(amounts.get("list_cost"))
    row["fallback_list_cost"] = Decimal()
    row["usage_seconds"] = None
    return row


def _detail_or_fallback_contributions(
    *,
    source: Mapping[str, Any],
    details: Sequence[Mapping[str, Any]],
    basis_key: str,
    materialization_version: str,
    calculated_at: datetime,
) -> list[dict[str, Any]]:
    source_list = _decimal(source.get("list_cost"))
    source_currency = _currency(source)
    for detail in details:
        if _currency(detail) != source_currency:
            raise RuntimeError(
                "Unmatched resource detail currency differs from its source fact: "
                f"{detail.get('currency')} != {source_currency}"
            )
    detail_total = sum((_decimal(detail.get("list_cost")) for detail in details), Decimal())
    # A positive source cost can expose only the detail share that is actually
    # present.  Cap at one to retain conservation for late/corrected exports.
    detail_share = (
        min(Decimal(1), detail_total / source_list)
        if source_list > 0 and detail_total > 0
        else Decimal()
    )
    result: list[dict[str, Any]] = []
    if detail_share:
        detail_amounts = {
            name: None if source.get(name) is None else _decimal(source.get(name)) * detail_share
            for name in _AMOUNTS
        }
        remaining = dict(detail_amounts)
        for index, detail in enumerate(details):
            weight = _decimal(detail.get("list_cost")) / detail_total
            row = _base_serving_row(
                source,
                detail=detail,
                identity_kind="resource_detail",
                basis_key=basis_key,
                materialization_version=materialization_version,
                calculated_at=calculated_at,
            )
            for name, amount in remaining.items():
                if amount is None:
                    row[name] = None
                elif index == len(details) - 1:
                    row[name] = amount
                else:
                    allocated = (detail_amounts[name] * weight).quantize(
                        _AMOUNT_QUANTUM, rounding=ROUND_HALF_UP
                    )
                    row[name] = allocated
                    remaining[name] = amount - allocated
            row["detail_list_cost"] = row["list_cost"] or Decimal()
            row["fallback_list_cost"] = Decimal()
            row["usage_seconds"] = _decimal_or_none(detail.get("usage_seconds"))
            result.append(row)

    assigned_list = sum((_decimal(row.get("list_cost")) for row in result), Decimal())
    # Keep every zero/negative source fact as explicit fallback.  Positive facts
    # retain their exact unrepresented residual after detail allocation.
    if source_list <= 0 or source_list - assigned_list != 0:
        fallback = _base_serving_row(
            source,
            detail=None,
            identity_kind="attribution_fallback",
            basis_key=basis_key,
            materialization_version=materialization_version,
            calculated_at=calculated_at,
        )
        for name in _AMOUNTS:
            amount = _decimal_or_none(source.get(name))
            assigned = sum((_decimal_or_none(row.get(name)) or Decimal() for row in result), Decimal())
            fallback[name] = None if amount is None else amount - assigned
        fallback["detail_list_cost"] = Decimal()
        fallback["fallback_list_cost"] = _decimal(fallback.get("list_cost"))
        source_usage = _decimal_or_none(source.get("usage_seconds"))
        detailed_usage = sum(
            (_decimal(row.get("usage_seconds")) for row in result), Decimal()
        )
        fallback["usage_seconds"] = (
            None if source_usage is None else max(source_usage - detailed_usage, Decimal())
        )
        result.append(fallback)
    return result


def _base_serving_row(
    source: Mapping[str, Any],
    *,
    detail: Mapping[str, Any] | None,
    identity_kind: str,
    basis_key: str,
    materialization_version: str,
    calculated_at: datetime,
) -> dict[str, Any]:
    vendor = str(source.get("vendor") or "")
    account_id = str(source.get("account_id") or "")
    currency = _currency(source)
    owner = str(source.get("owner") or "")
    source_identity = str(source.get("source_fact_hash") or source.get("dimension_hash") or "")
    if detail is not None:
        resource_id = str(detail.get("resource_id") or "") or None
        resource_name = str(detail.get("resource_name") or "(resource detail unavailable)")
        parent = str(detail.get("parent_resource_name") or "")
        service_name = detail.get("service_name") or source.get("service_name")
        group_identity = (
            (vendor, account_id, resource_id, currency)
            if resource_id is not None
            else (vendor, account_id, resource_name, parent, currency)
        )
        identity = (
            *group_identity,
            str(service_name or ""),
            source.get("group_id"),
            source.get("project"),
        )
        labels = detail.get("vendor_tags_json")
    else:
        resource_id = None
        resource_name = str(source.get("resource_name") or "(resource detail unavailable)")
        service_name = source.get("service_name")
        identity = (
            vendor,
            account_id,
            source_identity,
            "attribution_fallback",
            source.get("group_id"),
            source.get("project"),
            currency,
        )
        group_identity = identity
        labels = source.get("vendor_tags_json")
    return {
        "materialization_version": materialization_version,
        "basis_key": basis_key,
        "usage_date": source["usage_date"],
        "vendor": vendor,
        "account_id": account_id,
        "currency": currency,
        "owner_key": _sha256(owner),
        "owner": owner,
        "group_id": source.get("group_id"),
        "manager_id": source.get("manager_id"),
        "project": source.get("project"),
        "target_branch": source.get("target_branch"),
        "resource_group_key": _hash_identity(group_identity),
        "resource_key": _hash_identity(identity),
        "resource_name": resource_name,
        "resource_id": resource_id,
        "service_name": service_name,
        "resource_identity_kind": identity_kind,
        "representative_labels_json": labels,
        "metadata_variant_count": 1 if labels else 0,
        "detail_list_cost": Decimal(),
        "fallback_list_cost": Decimal(),
        "usage_seconds": None,
        "list_cost": Decimal(),
        "effective_cost": None,
        "credit_amount": None,
        "net_cost": None,
        "source_row_count": int(source.get("source_rows") or 1),
        "calculated_at": calculated_at,
    }


def _aggregate_contributions(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    label_variants: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    largest_label: dict[tuple[Any, ...], tuple[Decimal, str]] = {}
    for row in rows:
        # Keep this exactly aligned with uk_resource_serving_versioned. Resource
        # keys include the attribution scopes that the Dashboard can filter, so
        # a resource's contributions never collapse across teams or projects.
        key = (
            row["materialization_version"],
            row["basis_key"],
            row["vendor"],
            row["account_id"],
            row["usage_date"],
            row["owner_key"],
            row["resource_key"],
            row.get("target_branch"),
            _currency(row),
        )
        current = grouped.get(key)
        if current is None:
            current = dict(row)
            grouped[key] = current
        else:
            current["source_row_count"] += int(row.get("source_row_count") or 0)
            for name in ("detail_list_cost", "fallback_list_cost", "list_cost"):
                current[name] = _decimal(current.get(name)) + _decimal(row.get(name))
            for name in ("effective_cost", "credit_amount", "net_cost"):
                if current.get(name) is not None or row.get(name) is not None:
                    current[name] = _decimal(current.get(name)) + _decimal(row.get(name))
            if current.get("usage_seconds") is not None or row.get("usage_seconds") is not None:
                current["usage_seconds"] = _decimal(current.get("usage_seconds")) + _decimal(row.get("usage_seconds"))
        labels = str(row.get("representative_labels_json") or "")
        if labels:
            label_variants[key].add(labels)
            candidate = (abs(_decimal(row.get("list_cost"))), labels)
            if candidate > largest_label.get(key, (Decimal("-1"), "")):
                largest_label[key] = candidate
                current["representative_labels_json"] = row.get("representative_labels_json")
    for key, current in grouped.items():
        current["metadata_variant_count"] = len(label_variants[key])
    return tuple(grouped.values())


def _serving_schema_ready(engine: Engine) -> bool:
    required_tables = (
        "cost_attribution_daily",
        "cost_unmatched_resource_daily",
        "cost_resource_serving_daily",
        "cost_resource_serving_publication",
    )
    with engine.connect() as connection:
        return all(_table_exists(connection, table) for table in required_tables) and all(
            _table_has_column(connection, table, column)
            for table, column in (
                ("cost_attribution_daily", "currency"),
                ("cost_unmatched_resource_daily", "resource_id"),
                ("cost_unmatched_resource_daily", "currency"),
                ("cost_resource_serving_daily", "resource_id"),
                ("cost_resource_serving_daily", "project"),
                ("cost_resource_serving_daily", "currency"),
                ("cost_resource_serving_publication", "currency"),
            )
        )


def _table_exists(connection: Connection, table: str) -> bool:
    if connection.dialect.name == "sqlite":
        return connection.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table"),
            {"table": table},
        ).first() is not None
    return connection.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :table"
        ),
        {"table": table},
    ).first() is not None


def _table_has_column(connection: Connection, table: str, column: str) -> bool:
    if connection.dialect.name == "sqlite":
        return any(row[1] == column for row in connection.execute(text(f"PRAGMA table_info({table})")))
    return connection.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    ).first() is not None


def _source_windows(
    connection: Connection,
    *,
    start_date: date,
    end_date: date,
    vendor: str | None = None,
    account_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    if vendor is not None and account_id is not None:
        rows = connection.execute(
            _NATIVE_WINDOWS_FOR_SOURCE,
            {
                "start_date": start_date,
                "end_date": end_date,
                "vendor": vendor,
                "account_id": account_id,
            },
        ).mappings()
    else:
        rows = connection.execute(
            _NATIVE_WINDOWS, {"start_date": start_date, "end_date": end_date}
        ).mappings()
    windows = {
        (_as_date(row["usage_date"]), str(row["vendor"]), str(row["account_id"]))
        for row in rows
    }
    windows.update(
        (usage_date, refreshed_vendor, refreshed_account_id)
        for usage_date, refreshed_vendor, refreshed_account_id in _refreshed_empty_windows(
            connection, start_date=start_date, end_date=end_date
        )
        if vendor is None or (refreshed_vendor, refreshed_account_id) == (vendor, account_id)
    )
    return tuple({"usage_date": d, "vendor": v, "account_id": a} for d, v, a in sorted(windows))

def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _refreshed_empty_windows(
    connection: Connection, *, start_date: date, end_date: date
) -> set[tuple[date, str, str]]:
    try:
        states = connection.execute(_REFRESHED_ATTRIBUTION_STATES).mappings()
    except Exception:
        return set()
    windows: set[tuple[date, str, str]] = set()
    for state in states:
        try:
            watermark = state["watermark_json"]
            if isinstance(watermark, str):
                watermark = json.loads(watermark)
            if not isinstance(watermark, Mapping):
                continue
            refreshed_start = date.fromisoformat(str(watermark["start_date"]))
            refreshed_end = date.fromisoformat(str(watermark["end_date"]))
            vendor = str(watermark["vendor"])
            account_id = str(watermark["account_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        current = max(start_date, refreshed_start)
        while current <= min(end_date, refreshed_end):
            windows.add((current, vendor, account_id))
            current += timedelta(days=1)
    return windows


def _load_source_rows(
    connection: Connection, *, usage_date: date, vendor: str, account_id: str
) -> tuple[dict[str, Any], ...]:
    return tuple(dict(row) for row in connection.execute(_NATIVE_SOURCES, {
        "usage_date": usage_date, "vendor": vendor, "account_id": account_id
    }).mappings())

def _load_detail_rows(
    connection: Connection, *, usage_date: date, vendor: str, account_id: str
) -> tuple[dict[str, Any], ...]:
    return tuple(dict(row) for row in connection.execute(_DETAIL_ROWS, {
        "usage_date": usage_date, "vendor": vendor, "account_id": account_id
    }).mappings())


def _load_tencent_summary_rows(
    connection: Connection, *, usage_date: date, vendor: str, account_id: str
) -> tuple[dict[str, Any], ...]:
    return tuple(dict(row) for row in connection.execute(_TENCENT_SUMMARY_ROWS, {
        "usage_date": usage_date, "vendor": vendor, "account_id": account_id
    }).mappings())


def _replace_staged_window(
    engine: Engine,
    rows: Sequence[Mapping[str, Any]],
    *,
    materialization_version: str,
    basis_key: str,
    usage_date: date,
    vendor: str,
    account_id: str,
    batch_size: int,
) -> None:
    with engine.begin() as connection:
        connection.execute(_DELETE_STAGED, {
            "materialization_version": materialization_version, "basis_key": basis_key,
            "usage_date": usage_date, "vendor": vendor, "account_id": account_id,
        })
        for offset in range(0, len(rows), batch_size):
            batch = [dict(row) for row in rows[offset : offset + batch_size]]
            if connection.dialect.name == "sqlite":
                batch = bind_decimal_rows(batch)
            if batch:
                connection.execute(_INSERT_SERVING, batch)


def _publish_window(
    engine: Engine,
    *,
    basis_key: str,
    materialization_version: str,
    rows: Sequence[Mapping[str, Any]],
    source_rows: Sequence[Mapping[str, Any]],
    usage_date: date,
    vendor: str,
    account_id: str,
) -> None:
    with engine.begin() as connection:
        currencies = {_currency(row) for row in rows}
        if len(currencies) > 1:
            raise ValueError(
                f"Resource serving window mixes currencies: {sorted(currencies)}"
            )
        params = {
            "basis_key": basis_key, "vendor": vendor, "account_id": account_id,
            "usage_date": usage_date, "materialization_version": materialization_version,
            "detail_list_cost": sum((_decimal(row.get("detail_list_cost")) for row in rows), Decimal()),
            "total_list_cost": sum((_decimal(row.get("list_cost")) for row in source_rows), Decimal()),
            "source_row_count": sum((int(row.get("source_rows") or 1) for row in source_rows)),
            "currency": next(iter(currencies), "USD"),
        }
        connection.execute(
            _UPSERT_PUBLICATION_SQLITE if connection.dialect.name == "sqlite" else _UPSERT_PUBLICATION_MYSQL,
            bind_decimal_rows([params])[0] if connection.dialect.name == "sqlite" else params,
        )


def _assert_conserved(source_rows: Iterable[Mapping[str, Any]], serving_rows: Iterable[Mapping[str, Any]]) -> None:
    source = tuple(source_rows)
    serving = tuple(serving_rows)
    for currency in sorted({_currency(row) for row in source} | {_currency(row) for row in serving}):
        for amount in _AMOUNTS:
            expected = sum(
                (_decimal(row.get(amount)) for row in source if _currency(row) == currency),
                Decimal(),
            )
            actual = sum(
                (_decimal(row.get(amount)) for row in serving if _currency(row) == currency),
                Decimal(),
            )
            if abs(expected - actual) > _AMOUNT_QUANTUM:
                raise RuntimeError(
                    f"Resource serving does not conserve {currency} {amount}: {expected} != {actual}"
                )
    expected_list = sum((_decimal(row.get("list_cost")) for row in serving), Decimal())
    components = sum((_decimal(row.get("detail_list_cost")) + _decimal(row.get("fallback_list_cost")) for row in serving), Decimal())
    if abs(expected_list - components) > _AMOUNT_QUANTUM:
        raise RuntimeError("Resource serving detail and fallback list cost do not conserve")


def _decimal(value: Any) -> Decimal:
    return _decimal_or_none(value) or Decimal()


def _currency(row: Mapping[str, Any]) -> str:
    return str(row.get("currency") or "USD").upper()


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _hash_identity(values: tuple[Any, ...]) -> str:
    return _sha256(json.dumps([str(value or "") for value in values], separators=(",", ":")))


_REFRESHED_ATTRIBUTION_STATES = text("""
SELECT watermark_json
FROM cost_job_state
WHERE job_name LIKE 'refresh_cost_attribution_from_summary:%'
  AND last_status = 'succeeded'
""")
_NATIVE_WINDOWS = text("""
SELECT DISTINCT usage_date, vendor, account_id FROM cost_attribution_daily
WHERE usage_date BETWEEN :start_date AND :end_date ORDER BY usage_date, vendor, account_id
""")
_NATIVE_WINDOWS_FOR_SOURCE = text("""
SELECT DISTINCT usage_date, vendor, account_id FROM cost_attribution_daily
WHERE usage_date BETWEEN :start_date AND :end_date
  AND vendor = :vendor AND account_id = :account_id
ORDER BY usage_date
""")
_SOURCE_COLUMNS = """
usage_date, vendor, account_id, service_name, service, sku_name, region, org, repo, project,
target_branch, resource_name, vendor_tags_json, source_allocation_scope, owner, employee_id,
group_id, manager_id, usage_seconds, effective_cost, credit_amount, net_cost, currency,
source_rows, source_summary_row_hash
"""
_NATIVE_SOURCES = text(f"""
SELECT {_SOURCE_COLUMNS},
  CASE
    WHEN vendor = 'gcp' AND sku_name LIKE 'Compute Flexible Committed Use Discounts%'
      THEN 0
    ELSE list_cost
  END AS list_cost,
  dimension_hash AS source_fact_hash
FROM cost_attribution_daily
WHERE usage_date = :usage_date AND vendor = :vendor AND account_id = :account_id
ORDER BY dimension_hash
""")
_DETAIL_ROWS = text("""
SELECT source_summary_row_hash, resource_name, resource_id, parent_resource_name, service_name,
  vendor_tags_json, usage_seconds, list_cost, currency
FROM cost_unmatched_resource_daily
WHERE usage_date = :usage_date AND vendor = :vendor AND account_id = :account_id
  AND source_summary_row_hash IS NOT NULL AND source_summary_row_hash <> ''
ORDER BY source_row_hash
""")
_TENCENT_SUMMARY_ROWS = text("""
SELECT usage_date, vendor, account_id, service_name, resource_name, vendor_tags_json,
  service, project, list_cost, effective_cost, credit_amount, net_cost, currency,
  source_row_hash
FROM cost_bq_export_summary_daily
WHERE usage_date = :usage_date AND vendor = :vendor AND account_id = :account_id
ORDER BY source_row_hash
""")
_DELETE_STAGED = text("""
DELETE FROM cost_resource_serving_daily
WHERE materialization_version = :materialization_version AND basis_key = :basis_key
  AND usage_date = :usage_date AND vendor = :vendor AND account_id = :account_id
""")
_INSERT_SERVING = text("""
INSERT INTO cost_resource_serving_daily (
  materialization_version, basis_key, usage_date, vendor, account_id, owner_key, owner,
  group_id, manager_id, project, target_branch, resource_group_key, resource_key, resource_name, resource_id,
  service_name, resource_identity_kind, representative_labels_json, metadata_variant_count,
  detail_list_cost, fallback_list_cost, usage_seconds, list_cost, effective_cost, credit_amount,
  net_cost, currency, source_row_count, calculated_at
) VALUES (
  :materialization_version, :basis_key, :usage_date, :vendor, :account_id, :owner_key, :owner,
  :group_id, :manager_id, :project, :target_branch, :resource_group_key, :resource_key, :resource_name, :resource_id,
  :service_name, :resource_identity_kind, :representative_labels_json, :metadata_variant_count,
  :detail_list_cost, :fallback_list_cost, :usage_seconds, :list_cost, :effective_cost, :credit_amount,
  :net_cost, :currency, :source_row_count, :calculated_at
)
""")
_UPSERT_PUBLICATION_SQLITE = text("""
INSERT INTO cost_resource_serving_publication (
  basis_key, vendor, account_id, usage_date, active_materialization_version,
  detail_list_cost, total_list_cost, currency, source_row_count, tiflash_ready_at
) VALUES (
  :basis_key, :vendor, :account_id, :usage_date, :materialization_version,
  :detail_list_cost, :total_list_cost, :currency, :source_row_count, NULL
)
ON CONFLICT(basis_key, vendor, account_id, usage_date) DO UPDATE SET
  active_materialization_version = excluded.active_materialization_version,
  detail_list_cost = excluded.detail_list_cost, total_list_cost = excluded.total_list_cost,
  currency = excluded.currency, source_row_count = excluded.source_row_count,
  published_at = CURRENT_TIMESTAMP, tiflash_ready_at = NULL
""")
_UPSERT_PUBLICATION_MYSQL = text("""
INSERT INTO cost_resource_serving_publication (
  basis_key, vendor, account_id, usage_date, active_materialization_version,
  detail_list_cost, total_list_cost, currency, source_row_count, tiflash_ready_at
) VALUES (
  :basis_key, :vendor, :account_id, :usage_date, :materialization_version,
  :detail_list_cost, :total_list_cost, :currency, :source_row_count, NULL
)
ON DUPLICATE KEY UPDATE
  active_materialization_version = VALUES(active_materialization_version),
  detail_list_cost = VALUES(detail_list_cost), total_list_cost = VALUES(total_list_cost),
  currency = VALUES(currency), source_row_count = VALUES(source_row_count),
  published_at = CURRENT_TIMESTAMP, tiflash_ready_at = NULL
""")
