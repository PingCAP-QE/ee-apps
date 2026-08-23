from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.row_utils import bind_decimal_rows

LOG = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_WEIGHT = Decimal("0.0000000000000001")
_AMOUNTS = ("list_cost", "effective_cost", "credit_amount", "net_cost")
_NATIVE_RESIDUAL_SOURCE_SCOPES = {
    "eks_parent_residual",
    "eks_unallocated",
    "gke_residual",
}


@dataclass(frozen=True)
class MaterializeCostAllocationsSummary:
    start_date: date
    end_date: date
    allocation_version: str
    windows_seen: int
    rows_written: int
    dry_run: bool


def run_materialize_cost_allocations(
    engine: Engine,
    *,
    start_date: date,
    end_date: date,
    earliest_date: date,
    eq_root_lark_group_id: str,
    dry_run: bool = False,
    batch_size: int = 1_000,
    allocation_version: str | None = None,
    processing_start_date: date | None = None,
    processing_end_date: date | None = None,
    publish: bool = True,
    now: datetime | None = None,
) -> MaterializeCostAllocationsSummary:
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")
    if start_date != earliest_date:
        raise ValueError("start_date must equal the configured allocation earliest date")
    processing_start = processing_start_date or start_date
    processing_end = processing_end_date or end_date
    if not (start_date <= processing_start <= processing_end <= end_date):
        raise ValueError("processing dates must be within the complete history range")
    if not publish and allocation_version is None:
        raise ValueError("allocation_version is required when publication is disabled")
    resolved_at = (now or datetime.now(UTC)).replace(tzinfo=None)
    version = allocation_version or resolved_at.strftime("allocation_%Y%m%dT%H%M%S%f")

    with engine.begin() as connection:
        root_id = connection.execute(
            text(
                """
                SELECT id FROM roster_groups
                WHERE lark_group_id = :lark_group_id AND is_active = 1
                """
            ),
            {"lark_group_id": eq_root_lark_group_id},
        ).scalar_one_or_none()
        if root_id is None:
            raise ValueError(f"Active EQ roster group not found: {eq_root_lark_group_id!r}")
        eq_group_ids = {
            int(value)
            for value in connection.execute(
                text(
                    """
                    SELECT id FROM roster_groups
                    WHERE is_active = 1 AND path LIKE :root_path
                    """
                ),
                {"root_path": f"%/{root_id}/%"},
            ).scalars()
        }
        group_managers = {
            int(row["id"]): row["manager_id"]
            for row in connection.execute(
                text("SELECT id, manager_id FROM roster_groups WHERE is_active = 1")
            ).mappings()
        }
        roster_by_identity = _load_roster_identities(connection)
        latest_native_date = connection.execute(
            text("SELECT MAX(usage_date) FROM cost_attribution_daily")
        ).scalar_one_or_none()
        if isinstance(latest_native_date, str):
            latest_native_date = date.fromisoformat(latest_native_date)
        if latest_native_date is not None and end_date < latest_native_date:
            raise ValueError(
                f"end_date must cover the latest native cost date {latest_native_date}"
            )
        sources = tuple(
            connection.execute(
                text(
                    """
                    SELECT DISTINCT vendor, account_id FROM cost_attribution_daily
                    WHERE usage_date BETWEEN :start_date AND :end_date
                    ORDER BY vendor, account_id
                    """
                ),
                {"start_date": start_date, "end_date": end_date},
            ).mappings()
        )

    windows_seen = 0
    rows_written = 0
    candidates_seen = 0
    candidate_count = ((processing_end - processing_start).days + 1) * len(sources)
    started_at = time.monotonic()
    current = processing_start
    while current <= processing_end:
        for source in sources:
            candidates_seen += 1
            window_started_at = time.monotonic()
            params = {
                "usage_date": current,
                "vendor": source["vendor"],
                "account_id": source["account_id"],
            }
            with engine.begin() as connection:
                native = tuple(
                    dict(row) for row in connection.execute(_SELECT_NATIVE, params).mappings()
                )
                if not native:
                    _log_materialization_progress(
                        version=version,
                        usage_date=current,
                        vendor=str(source["vendor"]),
                        account_id=str(source["account_id"]),
                        candidates_seen=candidates_seen,
                        candidate_count=candidate_count,
                        rows_written=rows_written,
                        started_at=started_at,
                        window_started_at=window_started_at,
                    )
                    continue
                allocations = tuple(
                    dict(row)
                    for row in connection.execute(_SELECT_KUBERNETES, params).mappings()
                )
                mappings = tuple(
                    dict(row)
                    for row in connection.execute(_SELECT_KUBERNETES_SOURCES, params).mappings()
                )
            windows_seen += 1
            kubernetes = build_kubernetes_allocated_rows(
                native_rows=native,
                allocation_rows=allocations,
                source_mappings=mappings,
                roster_by_identity=roster_by_identity,
                allocation_version=version,
                roster_resolved_at=resolved_at,
            )
            eq = build_eq_allocated_rows(
                input_rows=native,
                native_rows=native,
                eq_group_ids=eq_group_ids,
                group_managers=group_managers,
                allocation_version=version,
                roster_resolved_at=resolved_at,
            )
            kubernetes_eq = build_eq_allocated_rows(
                input_rows=kubernetes,
                native_rows=native,
                eq_group_ids=eq_group_ids,
                group_managers=group_managers,
                allocation_version=version,
                roster_resolved_at=resolved_at,
                basis_key="kubernetes_eq_allocated",
            )
            perspectives = (*kubernetes, *eq, *kubernetes_eq)
            _assert_conserved(native, kubernetes)
            _assert_conserved(native, eq)
            _assert_conserved(kubernetes, kubernetes_eq)
            if not dry_run:
                _delete_staged_materialization_window(
                    engine,
                    params={**params, "allocation_version": version},
                    batch_size=batch_size,
                )
                for offset in range(0, len(perspectives), batch_size):
                    with engine.begin() as connection:
                        _write_materialized_rows(
                            connection, perspectives[offset : offset + batch_size]
                        )
            rows_written += 0 if dry_run else len(perspectives)
            _log_materialization_progress(
                version=version,
                usage_date=current,
                vendor=str(source["vendor"]),
                account_id=str(source["account_id"]),
                candidates_seen=candidates_seen,
                candidate_count=candidate_count,
                rows_written=rows_written,
                started_at=started_at,
                window_started_at=window_started_at,
            )
        current += timedelta(days=1)

    if publish and windows_seen and not dry_run:
        publish_materialized_cost_allocations(
            engine,
            start_date=start_date,
            end_date=end_date,
            earliest_date=earliest_date,
            allocation_version=version,
        )
    return MaterializeCostAllocationsSummary(
        start_date=start_date,
        end_date=end_date,
        allocation_version=version,
        windows_seen=windows_seen,
        rows_written=rows_written,
        dry_run=dry_run,
    )


def publish_materialized_cost_allocations(
    engine: Engine,
    *,
    start_date: date,
    end_date: date,
    earliest_date: date,
    allocation_version: str,
) -> None:
    if start_date != earliest_date:
        raise ValueError("start_date must equal the configured allocation earliest date")
    expected_windows = 0
    with engine.begin() as connection:
        latest_native_date = connection.execute(
            text("SELECT MAX(usage_date) FROM cost_attribution_daily")
        ).scalar_one_or_none()
        if isinstance(latest_native_date, str):
            latest_native_date = date.fromisoformat(latest_native_date)
        if latest_native_date is not None and end_date < latest_native_date:
            raise ValueError(f"end_date must cover the latest native cost date {latest_native_date}")
        sources = tuple(
            connection.execute(
                text(
                    """
                    SELECT DISTINCT vendor, account_id FROM cost_attribution_daily
                    WHERE usage_date BETWEEN :start_date AND :end_date
                    ORDER BY vendor, account_id
                    """
                ),
                {"start_date": start_date, "end_date": end_date},
            ).mappings()
        )

    current = start_date
    while current <= end_date:
        for source in sources:
            params = {
                "usage_date": current,
                "vendor": source["vendor"],
                "account_id": source["account_id"],
                "allocation_version": allocation_version,
            }
            with engine.begin() as connection:
                native = connection.execute(_SELECT_NATIVE_TOTALS, params).mappings().one()
                if int(native["row_count"]) == 0:
                    continue
                expected_windows += 1
                materialized = {
                    basis_key: connection.execute(
                        _SELECT_MATERIALIZED_TOTALS,
                        {**params, "basis_key": basis_key},
                    ).mappings().one()
                    for basis_key in (
                        "kubernetes_allocated",
                        "eq_allocated",
                        "kubernetes_eq_allocated",
                    )
                }
            for basis_key, row in materialized.items():
                if int(row["row_count"]) == 0:
                    raise ValueError(
                        f"Incomplete materialization window for {current} "
                        f"{source['vendor']}/{source['account_id']} {basis_key}"
                    )
                for amount in _AMOUNTS:
                    if abs(_decimal(row[amount]) - _decimal(native[amount])) > Decimal("0.005"):
                        raise ValueError(
                            f"Materialization conservation failed for {basis_key} "
                            f"{current} {source['vendor']}/{source['account_id']} {amount}"
                        )
        current += timedelta(days=1)

    if expected_windows == 0:
        raise ValueError("Cannot publish an empty materialization version")
    with engine.begin() as connection:
        statement = (
            _UPSERT_PUBLICATION_SQLITE
            if connection.dialect.name == "sqlite"
            else _UPSERT_PUBLICATION_MYSQL
        )
        connection.execute(statement, {"allocation_version": allocation_version})
    LOG.info(
        "materialization published: version=%s windows=%d range=%s..%s",
        allocation_version,
        expected_windows,
        start_date,
        end_date,
    )


def _delete_staged_materialization_window(
    engine: Engine,
    *,
    params: dict[str, Any],
    batch_size: int,
) -> None:
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            connection.execute(_DELETE_STAGED_WINDOW, params)
        return
    for basis_key in (
        "kubernetes_allocated",
        "eq_allocated",
        "kubernetes_eq_allocated",
    ):
        while True:
            with engine.begin() as connection:
                deleted = connection.execute(
                    _DELETE_STAGED_WINDOW_LIMITED,
                    {
                        **params,
                        "basis_key": basis_key,
                        "delete_batch_size": batch_size,
                    },
                ).rowcount
            if deleted < batch_size:
                break


def _log_materialization_progress(
    *,
    version: str,
    usage_date: date,
    vendor: str,
    account_id: str,
    candidates_seen: int,
    candidate_count: int,
    rows_written: int,
    started_at: float,
    window_started_at: float,
) -> None:
    elapsed = time.monotonic() - started_at
    percent = candidates_seen * 100 / candidate_count if candidate_count else 100
    eta = elapsed / candidates_seen * (candidate_count - candidates_seen) if candidates_seen else 0
    LOG.info(
        "materialization progress: version=%s date=%s source=%s/%s "
        "windows=%d/%d percent=%.1f rows=%d window_seconds=%.1f eta_seconds=%.0f",
        version,
        usage_date,
        vendor,
        account_id,
        candidates_seen,
        candidate_count,
        percent,
        rows_written,
        time.monotonic() - window_started_at,
        eta,
    )


def build_kubernetes_allocated_rows(
    *,
    native_rows: Iterable[Mapping[str, Any]],
    allocation_rows: Iterable[Mapping[str, Any]],
    source_mappings: Iterable[Mapping[str, Any]],
    roster_by_identity: Mapping[str, Mapping[str, Any]],
    allocation_version: str,
    roster_resolved_at: datetime,
) -> tuple[dict[str, Any], ...]:
    """Replace only fully reconciled source groups with Kubernetes allocations."""
    native = tuple(native_rows)
    source_by_hash: dict[tuple[Any, Any, Any, Any], Mapping[str, Any]] = {}
    duplicate_sources: set[tuple[Any, Any, Any, Any]] = set()
    for row in native:
        source_hash = row.get("source_summary_row_hash")
        if not source_hash:
            continue
        key = (*_boundary(row), source_hash)
        if key in source_by_hash:
            duplicate_sources.add(key)
        else:
            source_by_hash[key] = row

    mappings_by_group: dict[tuple[Any, Any, Any, Any], list[Mapping[str, Any]]] = defaultdict(list)
    for mapping in source_mappings:
        mappings_by_group[(*_boundary(mapping), mapping.get("allocation_group_hash"))].append(mapping)
    allocations_by_group: dict[tuple[Any, Any, Any, Any], list[Mapping[str, Any]]] = defaultdict(list)
    for row in allocation_rows:
        allocations_by_group[(*_boundary(row), row.get("allocation_group_hash"))].append(row)

    replaced: set[tuple[Any, Any, Any, Any]] = set()
    output: list[dict[str, Any]] = []
    for group_key in sorted(mappings_by_group, key=lambda key: tuple(str(value) for value in key)):
        mappings = mappings_by_group[group_key]
        allocations = sorted(
            allocations_by_group.get(group_key, []),
            key=lambda row: str(row.get("dimension_hash") or ""),
        )
        source_keys = [(*group_key[:3], mapping.get("source_summary_row_hash")) for mapping in mappings]
        if (
            not allocations
            or any(key in duplicate_sources or key not in source_by_hash for key in source_keys)
        ):
            continue
        sources = [source_by_hash[key] for key in source_keys]
        source_list = sum((_decimal(row.get("list_cost")) for row in sources), Decimal())
        mapped_list = sum((_decimal(row.get("source_list_cost")) for row in mappings), Decimal())
        allocated_list = sum((_decimal(row.get("list_cost")) for row in allocations), Decimal())
        if source_list == 0 or abs(source_list - mapped_list) > Decimal("0.005") or abs(
            source_list - allocated_list
        ) > Decimal("0.005"):
            continue

        weights = _weights(
            [(index, _decimal(row.get("list_cost"))) for index, row in enumerate(allocations)],
            allocated_list,
        )
        source_amounts = {
            name: (
                None
                if all(row.get(name) is None for row in sources)
                else sum((_decimal(row.get(name)) for row in sources), Decimal())
            )
            for name in _AMOUNTS
        }
        allocated_amounts = {
            name: _allocate_amount(amount, weights) for name, amount in source_amounts.items()
        }
        source = _common_source(sources)
        source["dimension_hash"] = hashlib.sha256(
            "|".join(sorted(str(row.get("dimension_hash") or "") for row in sources)).encode()
        ).hexdigest()
        for index, (allocation, weight) in enumerate(zip(allocations, weights, strict=True)):
            row = dict(source)
            identity = str(allocation.get("author") or "").lower()
            roster = roster_by_identity.get(identity, {})
            row.update(
                {
                    "region": allocation.get("cluster_location") or source.get("region"),
                    "namespace": allocation.get("namespace"),
                    "workload_name": allocation.get("workload_name"),
                    "workload_type": allocation.get("workload_type"),
                    "resource_name": allocation.get("workload_name") or source.get("resource_name"),
                    "author": allocation.get("author"),
                    "org": allocation.get("org") or source.get("org"),
                    "repo": allocation.get("repo") or source.get("repo"),
                    "target_branch": allocation.get("target_branch") or source.get("target_branch"),
                    "owner": roster.get("email") or roster.get("github_id"),
                    "employee_id": roster.get("employee_id"),
                    "group_id": roster.get("group_id"),
                    "manager_id": roster.get("manager_id"),
                    "attribution_key": (
                        f"employee:{roster['employee_id']}" if roster.get("employee_id") else None
                    ),
                    "attribution_source": "kubernetes_residual_allocation",
                    "attribution_status": "matched" if roster.get("employee_id") else "unattributed",
                    "allocate_method": allocation.get("allocation_method"),
                    "source_rows": 0,
                    "dimension_hash": allocation.get("dimension_hash"),
                }
            )
            for name in _AMOUNTS:
                row[name] = allocated_amounts[name][index]
            output.append(
                _serving_row(
                    row,
                    source=source,
                    basis_key="kubernetes_allocated",
                    allocation_stage="kubernetes_residual",
                    allocation_method=str(allocation.get("allocation_method") or "kubernetes_residual"),
                    allocation_weight=weight,
                    allocation_version=allocation_version,
                    roster_resolved_at=roster_resolved_at,
                )
            )
        replaced.update(source_keys)

    for row in native:
        key = (*_boundary(row), row.get("source_summary_row_hash"))
        if key not in replaced:
            output.append(
                _serving_row(
                    row,
                    basis_key="kubernetes_allocated",
                    allocation_stage="pass_through",
                    allocation_method="pass_through",
                    allocation_weight=Decimal(1),
                    allocation_version=allocation_version,
                    roster_resolved_at=roster_resolved_at,
                )
            )
    return tuple(output)


def build_eq_allocated_rows(
    *,
    input_rows: Iterable[Mapping[str, Any]],
    native_rows: Iterable[Mapping[str, Any]],
    eq_group_ids: set[int],
    group_managers: Mapping[int, int | None],
    allocation_version: str,
    roster_resolved_at: datetime,
    basis_key: str = "eq_allocated",
) -> tuple[dict[str, Any], ...]:
    """Charge current-EQ rows to same-day/account non-EQ direct-cost groups."""
    basis: dict[tuple[Any, Any, Any], dict[int, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for row in native_rows:
        group_id = row.get("group_id")
        list_cost = _decimal(row.get("list_cost"))
        if (
            group_id is not None
            and group_id in group_managers
            and group_id not in eq_group_ids
            and list_cost > 0
            and _is_native_direct(row)
        ):
            basis[_boundary(row)][int(group_id)] += list_cost

    output: list[dict[str, Any]] = []
    for source in input_rows:
        source_group_id = source.get("group_id")
        if source_group_id not in eq_group_ids:
            output.append(
                _serving_row(
                    source,
                    basis_key=basis_key,
                    allocation_stage="pass_through",
                    allocation_method="pass_through",
                    allocation_weight=Decimal(1),
                    allocation_version=allocation_version,
                    roster_resolved_at=roster_resolved_at,
                )
            )
            continue

        participants = basis.get(_boundary(source), {})
        denominator = sum(participants.values(), Decimal())
        if denominator <= 0:
            output.append(
                _serving_row(
                    source,
                    basis_key=basis_key,
                    allocation_stage="eq_chargeback",
                    allocation_method="eq_no_non_eq_direct_cost",
                    allocation_weight=Decimal(1),
                    allocation_version=allocation_version,
                    roster_resolved_at=roster_resolved_at,
                )
            )
            continue

        ordered = sorted(participants.items())
        weights = _weights(ordered, denominator)
        allocated_amounts = {
            name: _allocate_amount(_decimal_or_none(source.get(name)), weights)
            for name in _AMOUNTS
        }
        for index, ((group_id, _), weight) in enumerate(zip(ordered, weights, strict=True)):
            row = dict(source)
            row.update(
                {
                    "owner": None,
                    "employee_id": None,
                    "group_id": group_id,
                    "manager_id": group_managers.get(group_id),
                    "attribution_key": f"group:{group_id}",
                    "attribution_source": "eq_chargeback",
                    "attribution_status": "matched",
                    "allocate_method": "eq_direct_list_cost",
                    "source_rows": 0,
                }
            )
            for name in _AMOUNTS:
                row[name] = allocated_amounts[name][index]
            output.append(
                _serving_row(
                    row,
                    source=source,
                    basis_key=basis_key,
                    allocation_stage="eq_chargeback",
                    allocation_method="eq_direct_list_cost",
                    allocation_weight=weight,
                    allocation_version=allocation_version,
                    roster_resolved_at=roster_resolved_at,
                )
            )
    return tuple(output)


def _is_native_direct(row: Mapping[str, Any]) -> bool:
    return (
        str(row.get("source_allocation_scope") or "direct")
        not in _NATIVE_RESIDUAL_SOURCE_SCOPES
    )


def _boundary(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return row.get("usage_date"), row.get("vendor"), row.get("account_id")


def _common_source(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    source = dict(rows[0])
    if len({row.get("source_summary_row_hash") for row in rows}) > 1:
        source["source_summary_row_hash"] = None
    for field in (
        "service_name",
        "sku_name",
        "usage_type",
        "cost_driver_key",
        "region",
        "vendor_tags_json",
        "source_allocation_scope",
        "service",
        "project",
        "service_exec_id",
    ):
        values = {str(row.get(field)) for row in rows}
        if len(values) > 1:
            source[field] = None
    return source


def _weights(participants: list[tuple[int, Decimal]], denominator: Decimal) -> list[Decimal]:
    remaining = Decimal(1)
    result: list[Decimal] = []
    for _, amount in participants[:-1]:
        weight = (amount / denominator).quantize(_WEIGHT, rounding=ROUND_HALF_UP)
        result.append(weight)
        remaining -= weight
    result.append(remaining)
    return result


def _allocate_amount(amount: Decimal | None, weights: list[Decimal]) -> list[Decimal | None]:
    if amount is None:
        return [None] * len(weights)
    remaining = amount
    result: list[Decimal] = []
    for weight in weights[:-1]:
        allocated = (amount * weight).quantize(_CENT, rounding=ROUND_HALF_UP)
        result.append(allocated)
        remaining -= allocated
    result.append(remaining.quantize(_CENT, rounding=ROUND_HALF_UP))
    return result


def _serving_row(
    row: Mapping[str, Any],
    *,
    basis_key: str,
    allocation_stage: str,
    allocation_method: str,
    allocation_weight: Decimal,
    allocation_version: str,
    roster_resolved_at: datetime,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = source or row
    result = dict(row)
    result.update(
        {
            "basis_key": basis_key,
            "allocation_stage": allocation_stage,
            "source_fact_hash": source.get("dimension_hash"),
            "source_owner": source.get("owner"),
            "source_group_id": source.get("group_id"),
            "source_manager_id": source.get("manager_id"),
            "target_group_id": row.get("group_id"),
            "target_manager_id": row.get("manager_id"),
            "allocation_scope": _allocation_scope(row, allocation_stage, allocation_method),
            "allocation_method": allocation_method,
            "allocation_weight": allocation_weight,
            "allocation_version": allocation_version,
            "roster_resolved_at": roster_resolved_at,
        }
    )
    result["dimension_hash"] = _dimension_hash(result)
    return result


def _allocation_scope(
    row: Mapping[str, Any], allocation_stage: str, allocation_method: str
) -> str:
    if allocation_method == "eq_no_non_eq_direct_cost":
        return "residual_unallocated"
    if allocation_stage != "pass_through":
        return "redistributed"
    return str(
        row.get("allocation_scope")
        or ("direct" if _is_native_direct(row) else "residual_unallocated")
    )


def _dimension_hash(row: Mapping[str, Any]) -> str:
    payload = {
        key: str(
            (row.get("dimension_hash") if key == "input_dimension_hash" else row.get(key)) or ""
        )
        for key in (
            "basis_key",
            "allocation_version",
            "usage_date",
            "vendor",
            "account_id",
            "source_fact_hash",
            "input_dimension_hash",
            "target_group_id",
            "author",
            "namespace",
            "workload_name",
            "service_name",
            "sku_name",
            "allocation_method",
        )
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decimal(value: Any) -> Decimal:
    return _decimal_or_none(value) or Decimal()


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _load_roster_identities(connection: Connection) -> dict[str, dict[str, Any]]:
    rows = tuple(
        connection.execute(
            text(
                """
                SELECT id AS employee_id, email, github_id, group_id, manager_id
                FROM roster_employees WHERE is_active = 1
                """
            )
        ).mappings()
    )
    identities: dict[str, dict[str, Any]] = {}
    # Match Dashboard roster resolution: email wins over github_id on collisions.
    for field in ("github_id", "email"):
        for row in rows:
            identity = str(row[field] or "").strip().lower()
            if identity:
                identities[identity] = dict(row)
    return identities


def _assert_conserved(
    source_rows: Iterable[Mapping[str, Any]],
    output_rows: Iterable[Mapping[str, Any]],
) -> None:
    source = tuple(source_rows)
    output = tuple(output_rows)
    for amount in _AMOUNTS:
        source_total = sum((_decimal(row.get(amount)) for row in source), Decimal())
        output_total = sum((_decimal(row.get(amount)) for row in output), Decimal())
        if abs(source_total - output_total) > _CENT:
            raise RuntimeError(
                f"Cost allocation does not conserve {amount}: {source_total} != {output_total}"
            )


def _write_materialized_rows(
    connection: Connection,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    if not rows:
        return
    bound = bind_decimal_rows([dict(row) for row in rows]) if connection.dialect.name == "sqlite" else rows
    connection.execute(_INSERT_MATERIALIZED, bound)


_ATTRIBUTION_COLUMNS = """
  usage_date, vendor, account_id, service_name, sku_name, usage_type,
  cost_driver_key, region, org, repo, target_branch, resource_name,
  vendor_tags_json, source_allocation_scope, namespace, workload_name,
  workload_type, author, owner, service, project, service_exec_id,
  attribution_key, attribution_source, attribution_status, allocate_method,
  employee_id, group_id, manager_id, usage_seconds, list_cost, effective_cost,
  credit_amount, net_cost, source_rows, source_summary_row_hash, dimension_hash
"""

_SELECT_NATIVE = text(
    f"""
    SELECT {_ATTRIBUTION_COLUMNS}
    FROM cost_attribution_daily
    WHERE usage_date = :usage_date AND vendor = :vendor AND account_id = :account_id
    ORDER BY dimension_hash
    """
)
_SELECT_NATIVE_TOTALS = text(
    """
    SELECT COUNT(*) AS row_count,
      COALESCE(SUM(list_cost), 0) AS list_cost,
      COALESCE(SUM(effective_cost), 0) AS effective_cost,
      COALESCE(SUM(credit_amount), 0) AS credit_amount,
      COALESCE(SUM(net_cost), 0) AS net_cost
    FROM cost_attribution_daily
    WHERE usage_date = :usage_date AND vendor = :vendor AND account_id = :account_id
    """
)
_SELECT_MATERIALIZED_TOTALS = text(
    """
    SELECT COUNT(*) AS row_count,
      COALESCE(SUM(list_cost), 0) AS list_cost,
      COALESCE(SUM(effective_cost), 0) AS effective_cost,
      COALESCE(SUM(credit_amount), 0) AS credit_amount,
      COALESCE(SUM(net_cost), 0) AS net_cost
    FROM cost_allocation_daily
    WHERE basis_key = :basis_key
      AND allocation_version = :allocation_version
      AND usage_date = :usage_date AND vendor = :vendor AND account_id = :account_id
    """
)
_SELECT_KUBERNETES = text(
    """
    SELECT usage_date, vendor, account_id, cluster_location, allocation_scope,
      namespace, workload_name, workload_type, author, org, repo, target_branch,
      list_cost, allocation_weight, allocation_method, dimension_hash,
      source_summary_row_hash, allocation_group_hash
    FROM cost_kubernetes_workload_allocation_daily
    WHERE usage_date = :usage_date AND vendor = :vendor AND account_id = :account_id
    ORDER BY dimension_hash
    """
)
_SELECT_KUBERNETES_SOURCES = text(
    """
    SELECT usage_date, vendor, account_id, source_summary_row_hash,
      allocation_group_hash, source_list_cost
    FROM cost_kubernetes_workload_allocation_source_daily
    WHERE usage_date = :usage_date AND vendor = :vendor AND account_id = :account_id
    ORDER BY source_summary_row_hash
    """
)

_DELETE_STAGED_WINDOW = text(
    """
    DELETE FROM cost_allocation_daily
    WHERE allocation_version = :allocation_version
      AND usage_date = :usage_date
      AND vendor = :vendor
      AND account_id = :account_id
    """
)
_DELETE_STAGED_WINDOW_LIMITED = text(
    """
    DELETE FROM cost_allocation_daily
    WHERE basis_key = :basis_key
      AND allocation_version = :allocation_version
      AND usage_date = :usage_date
      AND vendor = :vendor
      AND account_id = :account_id
    LIMIT :delete_batch_size
    """
)
_INSERT_MATERIALIZED = text(
    """
    INSERT INTO cost_allocation_daily (
      basis_key, allocation_version, allocation_stage, usage_date, vendor,
      account_id, service_name, sku_name, usage_type, cost_driver_key, region,
      org, repo, target_branch, resource_name, vendor_tags_json,
      source_allocation_scope, namespace, workload_name, workload_type, author,
      owner, service, project, service_exec_id, attribution_key,
      attribution_source, attribution_status, allocate_method, employee_id,
      group_id, manager_id, usage_seconds, list_cost, effective_cost,
      credit_amount, net_cost, source_rows, source_summary_row_hash,
      source_fact_hash, source_owner, source_group_id, source_manager_id,
      target_group_id, target_manager_id, allocation_scope, allocation_method,
      allocation_weight, roster_resolved_at, dimension_hash
    ) VALUES (
      :basis_key, :allocation_version, :allocation_stage, :usage_date, :vendor,
      :account_id, :service_name, :sku_name, :usage_type, :cost_driver_key, :region,
      :org, :repo, :target_branch, :resource_name, :vendor_tags_json,
      :source_allocation_scope, :namespace, :workload_name, :workload_type, :author,
      :owner, :service, :project, :service_exec_id, :attribution_key,
      :attribution_source, :attribution_status, :allocate_method, :employee_id,
      :group_id, :manager_id, :usage_seconds, :list_cost, :effective_cost,
      :credit_amount, :net_cost, :source_rows, :source_summary_row_hash,
      :source_fact_hash, :source_owner, :source_group_id, :source_manager_id,
      :target_group_id, :target_manager_id, :allocation_scope, :allocation_method,
      :allocation_weight, :roster_resolved_at, :dimension_hash
    )
    """
)
_UPSERT_PUBLICATION_SQLITE = text(
    """
    INSERT INTO cost_allocation_publication (publication_name, active_allocation_version)
    VALUES ('dashboard', :allocation_version)
    ON CONFLICT(publication_name) DO UPDATE SET
      active_allocation_version = excluded.active_allocation_version,
      updated_at = CURRENT_TIMESTAMP
    """
)
_UPSERT_PUBLICATION_MYSQL = text(
    """
    INSERT INTO cost_allocation_publication (publication_name, active_allocation_version)
    VALUES ('dashboard', :allocation_version)
    ON DUPLICATE KEY UPDATE
      active_allocation_version = VALUES(active_allocation_version),
      updated_at = CURRENT_TIMESTAMP
    """
)
