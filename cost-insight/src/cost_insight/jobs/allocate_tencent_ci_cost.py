"""Coarsely allocate Tencent CI non-supernode cost by completed build weight."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.config import TENCENT_CI_SOURCE
from cost_insight.common.row_utils import bind_decimal_rows
from cost_insight.jobs.materialize_cost_allocations import _load_roster_identities
from cost_insight.jobs.materialize_resource_serving import run_materialize_resource_serving
from cost_insight.jobs.refresh_attribution_daily import (
    _DELETE_ATTRIBUTION_DAILY,
    _INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY,
)

VENDOR, ACCOUNT_ID = TENCENT_CI_SOURCE
_PRODUCT_CODE_KEY = "__tencent_product_code"
_SUPERNODE_PREFIX = "sp_eks_supernode"
_AMOUNT_FIELDS = ("list_cost", "effective_cost", "credit_amount", "net_cost")
_AMOUNT_QUANTUM = Decimal("0.000000001")
_BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class TencentCiAllocationSummary:
    start_date: date
    end_date: date
    days_processed: int
    rows_written: int
    dry_run: bool


def run_allocate_tencent_ci_cost(
    engine: Engine,
    *,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> TencentCiAllocationSummary:
    """Replace the Tencent CI account's attribution one Beijing day at a time."""
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")

    days_processed = 0
    rows_written = 0
    current = start_date
    while current <= end_date:
        if dry_run:
            with engine.connect() as connection:
                summary_rows = _summary_rows(connection, current)
                product_codes = _product_codes(summary_rows, current)
                shared_rows = _shared_rows(connection, current, summary_rows, product_codes)
            rows_written += len(shared_rows)
        else:
            with engine.begin() as connection:
                summary_rows = _summary_rows(connection, current)
                product_codes = _product_codes(summary_rows, current)
                non_supernode_hashes = _non_supernode_hashes(summary_rows, product_codes)
                connection.execute(_DELETE_ATTRIBUTION_DAILY, _params(current))
                connection.execute(_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY, _params(current))
                for offset in range(0, len(non_supernode_hashes), 500):
                    connection.execute(
                        _DELETE_SHARED_DIRECT_ROWS,
                        {**_params(current), "source_hashes": non_supernode_hashes[offset : offset + 500]},
                    )
                shared_rows = _shared_rows(connection, current, summary_rows, product_codes)
                if shared_rows:
                    connection.execute(
                        _INSERT_SHARED_ROW,
                        bind_decimal_rows(shared_rows)
                        if connection.dialect.name == "sqlite"
                        else shared_rows,
                    )
            run_materialize_resource_serving(
                engine,
                start_date=current,
                end_date=current,
                vendor=VENDOR,
                account_id=ACCOUNT_ID,
            )
            rows_written += len(shared_rows)
        days_processed += 1
        current += timedelta(days=1)

    return TencentCiAllocationSummary(start_date, end_date, days_processed, rows_written, dry_run)


def _summary_rows(connection: Connection, usage_date: date) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(_SELECT_SUMMARY_ROWS, _params(usage_date)).mappings()
    return tuple(dict(row) for row in rows)


def _non_supernode_hashes(
    rows: tuple[dict[str, Any], ...], product_codes: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(
        str(row["source_row_hash"])
        for row, product_code in zip(rows, product_codes, strict=True)
        if not product_code.startswith(_SUPERNODE_PREFIX)
    )


def _shared_rows(
    connection: Connection,
    usage_date: date,
    summary_rows: tuple[dict[str, Any], ...],
    product_codes: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    pools: dict[tuple[str, str | None, str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for row, product_code in zip(summary_rows, product_codes, strict=True):
        if not product_code.startswith(_SUPERNODE_PREFIX):
            service = row.get("service") or None
            pools[
                (
                    str(row.get("currency") or "USD").upper(),
                    row.get("service_name") or None,
                    service,
                    row.get("project") or service,
                )
            ].append(row)

    weights = _build_weights(connection, usage_date)
    return tuple(
        allocated
        for (currency, service_name, service, project), pool in sorted(
            pools.items(), key=lambda item: tuple(value or "" for value in item[0])
        )
        for allocated in _allocate_pool(
            usage_date,
            currency,
            pool,
            weights,
            service_name=service_name,
            service=service,
            project=project,
        )
    )


def _product_codes(rows: tuple[dict[str, Any], ...], usage_date: date) -> tuple[str, ...]:
    product_codes = []
    for row in rows:
        tags = row.get("vendor_tags_json")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Tencent {usage_date} has invalid vendor tags") from exc
        product_code = tags.get(_PRODUCT_CODE_KEY) if isinstance(tags, Mapping) else None
        if not isinstance(product_code, str) or not product_code.strip():
            raise ValueError(
                f"Tencent {usage_date} source {row['source_row_hash']} lacks {_PRODUCT_CODE_KEY}; reimport first"
            )
        product_codes.append(product_code.strip())
    return tuple(product_codes)


def _build_weights(
    connection: Connection, usage_date: date
) -> dict[tuple[int | None, str | None, str | None], dict[str, Any]]:
    start = datetime.combine(usage_date, time.min, tzinfo=_BEIJING).astimezone(UTC).replace(tzinfo=None)
    end = datetime.combine(usage_date + timedelta(days=1), time.min, tzinfo=_BEIJING).astimezone(UTC).replace(
        tzinfo=None
    )
    # Source rows use the direct-summary INSERT; builds reuse the existing active roster lookup.
    roster = _load_roster_identities(connection)
    weights: dict[tuple[int | None, str | None, str | None], dict[str, Any]] = {}
    for build in connection.execute(_SELECT_BUILDS, {"start": start, "end": end}).mappings():
        employee = roster.get(str(build.get("author") or "").strip().lower())
        employee_id = int(employee["employee_id"]) if employee else None
        org = build.get("org")
        repo = build.get("repo")
        item = weights.setdefault(
            (employee_id, org, repo),
            {
                "weight": Decimal(),
                "usage_seconds": Decimal(),
                "org": org,
                "repo": repo,
                "owner": employee.get("email") if employee else None,
                "group_id": employee.get("group_id") if employee else None,
                "manager_id": employee.get("manager_id") if employee else None,
            },
        )
        seconds = max(_decimal(build.get("run_seconds")), _decimal(build.get("total_seconds")), Decimal())
        item["weight"] += Decimal(1) + seconds / Decimal(3600)
        item["usage_seconds"] += seconds
    return weights


def _allocate_pool(
    usage_date: date,
    currency: str,
    pool: list[dict[str, Any]],
    weights: dict[tuple[int | None, str | None, str | None], dict[str, Any]],
    *,
    service_name: str | None,
    service: str | None,
    project: str | None,
) -> tuple[dict[str, Any], ...]:
    amounts = {field: _sum_amount(pool, field) for field in _AMOUNT_FIELDS}
    participants = sorted(
        weights.items(),
        key=lambda value: (
            value[0][0] is None,
            value[0][0] or 0,
            value[0][1] is not None,
            value[0][1] or "",
            value[0][2] is not None,
            value[0][2] or "",
        ),
    ) or [
        ((None, None, None), {"weight": Decimal(), "usage_seconds": Decimal(), "org": None, "repo": None})
    ]
    total_weight = sum((item["weight"] for _, item in participants), Decimal())
    rows: list[dict[str, Any]] = []
    remaining = dict(amounts)
    for index, (key, item) in enumerate(participants):
        employee_id = key[0]
        allocated = _allocated_amounts(
            amounts, remaining, item["weight"], total_weight, index == len(participants) - 1
        )
        rows.append(
            _shared_row(
                usage_date,
                currency,
                len(pool),
                employee_id,
                item,
                allocated,
                service_name=service_name,
                service=service,
                project=project,
                residual=employee_id is None,
            )
        )
    return tuple(rows)


def _allocated_amounts(
    amounts: Mapping[str, Decimal | None],
    remaining: dict[str, Decimal | None],
    weight: Decimal,
    total_weight: Decimal,
    last_participant: bool,
) -> dict[str, Decimal | None]:
    allocated: dict[str, Decimal | None] = {}
    for field, amount in amounts.items():
        if amount is None:
            allocated[field] = None
        elif last_participant:
            allocated[field] = remaining[field]
        else:
            allocated[field] = (amount * weight / total_weight).quantize(
                _AMOUNT_QUANTUM, rounding=ROUND_HALF_UP
            )
        if allocated[field] is not None:
            remaining[field] -= allocated[field]
    return allocated


def _shared_row(
    usage_date: date,
    currency: str,
    source_rows: int,
    employee_id: int | None,
    weight: Mapping[str, Any],
    amounts: Mapping[str, Decimal | None],
    *,
    service_name: str | None,
    service: str | None,
    project: str | None,
    residual: bool,
) -> dict[str, Any]:
    attribution_key = "unattributed" if residual else f"employee:{employee_id}"
    row = {
        "usage_date": usage_date,
        "vendor": VENDOR,
        "account_id": ACCOUNT_ID,
        "service_name": service_name,
        "sku_name": None,
        "usage_type": None,
        "cost_driver_key": None,
        "region": None,
        "org": weight["org"],
        "repo": weight["repo"],
        "target_branch": None,
        "resource_name": None,
        "vendor_tags_json": None,
        "source_allocation_scope": "tencent_ci_shared",
        "namespace": None,
        "workload_name": None,
        "workload_type": None,
        "author": None,
        "owner": None if residual else weight["owner"],
        "service": service,
        "project": project,
        "service_exec_id": None,
        "attribution_key": attribution_key,
        "attribution_source": "tencent_ci_build_weighted_residual" if residual else "tencent_ci_build_weighted",
        "attribution_status": "unattributed" if residual else "matched",
        "allocate_method": "tencent_ci_build_weight_v1",
        "employee_id": employee_id,
        "group_id": None if residual else weight["group_id"],
        "manager_id": None if residual else weight["manager_id"],
        "usage_seconds": weight["usage_seconds"],
        "list_cost": amounts["list_cost"],
        "effective_cost": amounts["effective_cost"],
        "credit_amount": amounts["credit_amount"],
        "net_cost": amounts["net_cost"],
        "currency": currency,
        "source_rows": source_rows,
        "source_summary_row_hash": None,
    }
    row["dimension_hash"] = hashlib.sha256(
        json.dumps(
            {
                key: str(row[key])
                for key in ("usage_date", "vendor", "account_id", "currency", "attribution_key")
            }
            | {key: row[key] for key in ("service_name", "org", "repo", "service", "project")},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return row


def _sum_amount(rows: list[dict[str, Any]], field: str) -> Decimal | None:
    values = [row.get(field) for row in rows]
    return None if all(value is None for value in values) else sum((_decimal(value) for value in values), Decimal())


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value if value not in (None, "") else 0))


def _params(usage_date: date) -> dict[str, Any]:
    return {"vendor": VENDOR, "account_id": ACCOUNT_ID, "start_date": usage_date, "end_date": usage_date}


_SELECT_SUMMARY_ROWS = text(
    """
    SELECT * FROM cost_bq_export_summary_daily
    WHERE vendor=:vendor AND account_id=:account_id AND usage_date=:start_date
    ORDER BY source_row_hash
    """
)
_SELECT_BUILDS = text(
    """
    SELECT author, org, repo, run_seconds, total_seconds FROM ci_l1_builds
    WHERE cloud_phase='TENCENT' AND completion_time IS NOT NULL
      AND start_time >= :start AND start_time < :end
    """
)
_DELETE_SHARED_DIRECT_ROWS = text(
    """
    DELETE FROM cost_attribution_daily
    WHERE vendor=:vendor AND account_id=:account_id AND usage_date=:start_date
      AND source_summary_row_hash IN :source_hashes
    """
).bindparams(bindparam("source_hashes", expanding=True))
_INSERT_SHARED_ROW = text(
    """
    INSERT INTO cost_attribution_daily (
      usage_date, vendor, account_id, service_name, sku_name, usage_type, cost_driver_key, region,
      org, repo, target_branch, resource_name, vendor_tags_json, source_allocation_scope, namespace,
      workload_name, workload_type, author, owner, service, project, service_exec_id, attribution_key,
      attribution_source, attribution_status, allocate_method, employee_id, group_id, manager_id,
      usage_seconds, list_cost, effective_cost, credit_amount, net_cost, currency, source_rows,
      dimension_hash, source_summary_row_hash
    ) VALUES (
      :usage_date, :vendor, :account_id, :service_name, :sku_name, :usage_type, :cost_driver_key, :region,
      :org, :repo, :target_branch, :resource_name, :vendor_tags_json, :source_allocation_scope, :namespace,
      :workload_name, :workload_type, :author, :owner, :service, :project, :service_exec_id, :attribution_key,
      :attribution_source, :attribution_status, :allocate_method, :employee_id, :group_id, :manager_id,
      :usage_seconds, :list_cost, :effective_cost, :credit_amount, :net_cost, :currency, :source_rows,
      :dimension_hash, :source_summary_row_hash
    )
    """
)
