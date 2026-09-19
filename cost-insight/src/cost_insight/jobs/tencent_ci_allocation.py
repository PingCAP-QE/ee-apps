"""Tencent CI native cost allocation V1.

The allocation math is deliberately kept independent from SQL.  The job layer
only snapshots its inputs, stages complete projection rows, and atomically
selects a staged version for one billing day.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.row_utils import bind_decimal_rows

ACCOUNT_ID = "100050658403"
VENDOR = "tencent"
WEIGHT_MODEL = "build-count-duration-v1"
WEIGHT_VERSION = "v1"
ALGORITHM_VERSION = "tencent-ci-native-v1"
AMOUNT_FIELDS = ("list_cost", "effective_cost", "credit_amount", "net_cost")
_AMOUNT_QUANTUM = Decimal("0.000000001")
_BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ResolvedIdentity:
    employee_id: int | None
    email: str | None
    group_id: int | None
    manager_id: int | None
    path: str

    @property
    def matched(self) -> bool:
        return self.employee_id is not None


@dataclass(frozen=True)
class TencentAllocationSummary:
    allocation_version: str
    start_date: date
    end_date: date
    days_validated: int
    projection_rows: int


@dataclass(frozen=True)
class TencentPublicationSummary:
    allocation_version: str
    usage_dates: tuple[date, ...]
    published_days: int
    rollback: bool = False


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_value,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def tencent_code_tuple(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return tuple(
        str(row.get(key) or "").strip()
        for key in (
            "tencent_business_code",
            "tencent_product_code",
            "tencent_component_code",
            "tencent_item_code",
        )
    )  # type: ignore[return-value]


def normalize_classification_rules(value: Any) -> tuple[dict[str, str], ...]:
    """Validate exact four-code rules; absent rules never imply non-supernode."""
    if not isinstance(value, list):
        raise ValueError("Tencent classification rules must be a JSON list")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("Tencent classification rules must contain objects")
        result = str(item.get("classification") or "").strip()
        if result not in {"supernode", "non_supernode"}:
            raise ValueError("Tencent rule classification must be supernode or non_supernode")
        rule = {
            "business_code": str(item.get("business_code") or "").strip(),
            "product_code": str(item.get("product_code") or "").strip(),
            "component_code": str(item.get("component_code") or "").strip(),
            "item_code": str(item.get("item_code") or "").strip(),
            "classification": result,
        }
        key = (
            rule["business_code"],
            rule["product_code"],
            rule["component_code"],
            rule["item_code"],
        )
        if not all(key):
            raise ValueError("Tencent classification rules require all four stable codes")
        if key in seen:
            raise ValueError(f"Duplicate Tencent classification code tuple: {key!r}")
        seen.add(key)
        normalized.append(rule)
    return tuple(sorted(normalized, key=lambda item: tuple(item[key] for key in item)))


def classify_tencent_row(
    row: Mapping[str, Any], rules: Iterable[Mapping[str, str]]
) -> str:
    code = tencent_code_tuple(row)
    for rule in rules:
        if code == (
            rule["business_code"],
            rule["product_code"],
            rule["component_code"],
            rule["item_code"],
        ):
            return rule["classification"]
    return "unclassified"


def build_duration_seconds(row: Mapping[str, Any]) -> Decimal:
    run = _decimal(row.get("run_seconds"))
    if run > 0:
        return run
    total = _decimal(row.get("total_seconds"))
    return total if total > 0 else Decimal()


def build_weight(row: Mapping[str, Any]) -> Decimal:
    return Decimal(1) + build_duration_seconds(row) / Decimal(3600)


def beijing_usage_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError(f"Unsupported build timestamp: {value!r}")
    utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return utc_value.astimezone(_BEIJING).date()


def canonical_build_fingerprint(rows: Iterable[Mapping[str, Any]]) -> str:
    values = []
    for row in rows:
        values.append(
            {
                "source_prow_job_id": str(row.get("source_prow_job_id") or ""),
                "start_time": _time_text(row.get("start_time")),
                "completion_time": _time_text(row.get("completion_time")),
                "cloud_phase": str(row.get("cloud_phase") or ""),
                "author": str(row.get("author") or ""),
                "org": str(row.get("org") or ""),
                "repo": str(row.get("repo") or ""),
                "job_name": str(row.get("job_name") or ""),
                "run_seconds": _decimal_text(_decimal(row.get("run_seconds"))),
                "total_seconds": _decimal_text(_decimal(row.get("total_seconds"))),
                "participates": bool(row.get("participates")),
            }
        )
    return content_hash(sorted(values, key=canonical_json))


def canonical_ledger_fingerprint(rows: Iterable[Mapping[str, Any]]) -> str:
    values = [
        {
            "source_row_hash": str(row.get("source_row_hash") or ""),
            "currency": str(row.get("currency") or "").upper(),
            "classification": str(row.get("tencent_cost_class") or ""),
            "classification_version": str(row.get("tencent_classification_version") or ""),
            "codes": tencent_code_tuple(row),
            "amounts": {name: _nullable_decimal_text(row.get(name)) for name in AMOUNT_FIELDS},
            "service_name": row.get("service_name"),
            "cost_driver_key": row.get("cost_driver_key"),
        }
        for row in rows
    ]
    return content_hash(sorted(values, key=canonical_json))


def snapshot_roster(rows: Iterable[Mapping[str, Any]]) -> tuple[str, str]:
    """Return content hash and canonical snapshot JSON for active roster identities."""
    entries = [
        {
            "employee_id": int(row["employee_id"]),
            "email": _text_or_none(row.get("email")),
            "github_id": _text_or_none(row.get("github_id")),
            "en_name": _text_or_none(row.get("en_name")),
            "group_id": int(row["group_id"]),
            "manager_id": int(row["manager_id"]) if row.get("manager_id") is not None else None,
        }
        for row in rows
    ]
    payload = canonical_json(sorted(entries, key=lambda item: item["employee_id"]))
    return hashlib.sha256(payload.encode()).hexdigest(), payload


def resolve_identity(
    identity: str | None,
    *,
    roster: Iterable[Mapping[str, Any]],
    aliases: Mapping[str, int] | None = None,
) -> ResolvedIdentity:
    """Resolve one non-empty identity without selecting an ambiguous candidate."""
    identity = _text_or_none(identity)
    if identity is None:
        return ResolvedIdentity(None, None, None, None, "unmatched")
    candidates = tuple(roster)
    alias_id = (aliases or {}).get(identity.lower())
    if alias_id is not None:
        match = [row for row in candidates if int(row["employee_id"]) == int(alias_id)]
        if len(match) == 1:
            return _resolved(match[0], "alias")
        return ResolvedIdentity(None, None, None, None, "unmatched")
    exact_github = _identity_matches(candidates, "github_id", identity)
    if len(exact_github) > 1:
        return ResolvedIdentity(None, None, None, None, "unmatched")
    if exact_github:
        return _resolved(exact_github[0], "github")
    exact_email = _identity_matches(candidates, "email", identity)
    if len(exact_email) > 1:
        return ResolvedIdentity(None, None, None, None, "unmatched")
    if exact_email:
        return _resolved(exact_email[0], "email")
    local = identity.lower().split("@", 1)[0]
    fallback = [
        row
        for row in candidates
        if local
        and local
        in {
            _normalized_identity(row.get("github_id")),
            _normalized_identity(_email_local(row.get("email"))),
            _normalized_identity(row.get("en_name")),
        }
    ]
    unique = {int(row["employee_id"]): row for row in fallback}
    if len(unique) == 1:
        return _resolved(next(iter(unique.values())), "normalized")
    return ResolvedIdentity(None, None, None, None, "unmatched")


def resolve_direct_identity(
    row: Mapping[str, Any], *, roster: Iterable[Mapping[str, Any]], aliases: Mapping[str, int]
) -> ResolvedIdentity:
    for field, prefix in (("owner", "owner"), ("author", "author")):
        identity = _text_or_none(row.get(field))
        if identity is not None:
            resolved = resolve_identity(identity, roster=roster, aliases=aliases)
            return ResolvedIdentity(
                resolved.employee_id,
                resolved.email,
                resolved.group_id,
                resolved.manager_id,
                f"{prefix}_{resolved.path}",
            )
    return ResolvedIdentity(None, None, None, None, "unmatched")


def build_tencent_projection(
    *,
    ledger_rows: Iterable[Mapping[str, Any]],
    builds: Iterable[Mapping[str, Any]],
    roster: Iterable[Mapping[str, Any]],
    aliases: Mapping[str, int],
    allocation_version: str,
    classification_version: str,
) -> tuple[tuple[dict[str, Any], ...], str, dict[str, Any]]:
    """Build a complete, amount-conserving V1 projection for one Beijing day."""
    ledger = tuple(ledger_rows)
    unclassified = [row for row in ledger if row.get("tencent_cost_class") == "unclassified"]
    if unclassified:
        details = [
            {"codes": tencent_code_tuple(row), "net_cost": _nullable_decimal_text(row.get("net_cost"))}
            for row in unclassified
        ]
        raise ValueError(f"Tencent usage date has unclassified cost rows: {canonical_json(details)}")
    unknown = [row for row in ledger if row.get("tencent_cost_class") not in {"supernode", "non_supernode"}]
    if unknown:
        raise ValueError("Tencent usage date has missing or invalid classification")

    roster_entries = tuple(roster)
    completed_builds = tuple(builds)
    participant_weights: dict[tuple[Any, ...], Decimal] = defaultdict(Decimal)
    unmatched_weight = Decimal()
    unmatched_count = 0
    for build in completed_builds:
        weight = build_weight(build)
        resolved = resolve_identity(build.get("author"), roster=roster_entries, aliases=aliases)
        if not resolved.matched:
            unmatched_weight += weight
            unmatched_count += 1
            continue
        key = (
            resolved.employee_id,
            build.get("author"),
            build.get("org"),
            build.get("repo"),
            resolved.email,
            resolved.group_id,
            resolved.manager_id,
        )
        participant_weights[key] += weight
    total_weight = sum(participant_weights.values(), unmatched_weight)

    projection: list[dict[str, Any]] = []
    for row in ledger:
        if row["tencent_cost_class"] == "supernode":
            projection.append(
                _direct_projection(
                    row,
                    resolve_direct_identity(row, roster=roster_entries, aliases=aliases),
                    allocation_version=allocation_version,
                    classification_version=classification_version,
                )
            )

    pools = _pools(
        (row for row in ledger if row["tencent_cost_class"] == "non_supernode"),
        allocation_version=allocation_version,
        classification_version=classification_version,
    )
    for pool in pools:
        projection.extend(
            _shared_pool_projection(
                pool,
                participant_weights=participant_weights,
                total_weight=total_weight,
                unmatched_weight=unmatched_weight,
                unmatched_count=unmatched_count,
                allocation_version=allocation_version,
                classification_version=classification_version,
            )
        )

    _assert_projection_conserved(ledger, projection)
    pool_fingerprint = content_hash(
        [
            {"source_pool_key": pool["source_pool_key"], "source_fingerprint": pool["source_fingerprint"]}
            for pool in pools
        ]
    )
    statistics = {
        "completed_build_count": len(completed_builds),
        "matched_build_count": len(completed_builds) - unmatched_count,
        "unmatched_build_count": unmatched_count,
        "total_weight": _decimal_text(total_weight),
        "unmatched_weight": _decimal_text(unmatched_weight),
        "pool_count": len(pools),
        "projection_row_count": len(projection),
    }
    return tuple(projection), pool_fingerprint, statistics


def _pools(
    rows: Iterable[Mapping[str, Any]],
    *,
    allocation_version: str,
    classification_version: str,
) -> tuple[dict[str, Any], ...]:
    grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                row.get("usage_date"),
                str(row.get("currency") or "").upper(),
                row.get("service_name"),
                row.get("cost_driver_key"),
            )
        ].append(row)
    pools = []
    for key in sorted(grouped, key=lambda item: tuple(str(value) for value in item)):
        source_rows = grouped[key]
        source_hashes = sorted(str(row.get("source_row_hash") or "") for row in source_rows)
        source_fingerprint = content_hash(source_hashes)
        source_pool_key = content_hash(
            {
                "allocation_version": allocation_version,
                "classification_version": classification_version,
                "boundary": key,
                "source_fingerprint": source_fingerprint,
            }
        )
        amounts = {name: _sum_optional_amount(source_rows, name) for name in AMOUNT_FIELDS}
        pools.append(
            {
                "usage_date": key[0],
                "currency": key[1],
                "service_name": key[2],
                "cost_driver_key": key[3],
                "source_rows": len(source_rows),
                "source_pool_key": source_pool_key,
                "source_fingerprint": source_fingerprint,
                "amounts": amounts,
            }
        )
    return tuple(pools)


def _direct_projection(
    source: Mapping[str, Any],
    resolved: ResolvedIdentity,
    *,
    allocation_version: str,
    classification_version: str,
) -> dict[str, Any]:
    row = _projection_base(source)
    row.update(
        {
            "source_allocation_scope": "tencent_l3_direct",
            "owner": resolved.email,
            "attribution_key": f"employee:{resolved.employee_id}" if resolved.matched else "unattributed",
            "attribution_source": f"tencent_l3_direct_{resolved.path}",
            "attribution_status": "matched" if resolved.matched else "unattributed",
            "employee_id": resolved.employee_id,
            "group_id": resolved.group_id,
            "manager_id": resolved.manager_id,
            "source_rows": 1,
            "source_summary_row_hash": source.get("source_row_hash"),
            "source_pool_key": None,
            "allocation_weight": None,
            "allocation_version": allocation_version,
            "classification_version": classification_version,
            "weight_model": WEIGHT_MODEL,
            "weight_version": WEIGHT_VERSION,
        }
    )
    row["dimension_hash"] = _projection_hash(row)
    return row


def _shared_pool_projection(
    pool: Mapping[str, Any],
    *,
    participant_weights: Mapping[tuple[Any, ...], Decimal],
    total_weight: Decimal,
    unmatched_weight: Decimal,
    unmatched_count: int,
    allocation_version: str,
    classification_version: str,
) -> tuple[dict[str, Any], ...]:
    participants = sorted(participant_weights.items(), key=lambda item: tuple(str(value) for value in item[0]))
    amounts = dict(pool["amounts"])
    metadata = canonical_json(
        {
            "source_pool_key": pool["source_pool_key"],
            "classification_version": classification_version,
            "raw_source_fingerprint": pool["source_fingerprint"],
        }
    )
    base = {
        "usage_date": pool["usage_date"],
        "vendor": VENDOR,
        "account_id": ACCOUNT_ID,
        "service_name": pool["service_name"],
        "sku_name": None,
        "usage_type": None,
        "cost_driver_key": pool["cost_driver_key"],
        "region": None,
        "target_branch": None,
        "resource_name": None,
        "vendor_tags_json": metadata,
        "source_allocation_scope": "tencent_ci_shared",
        "namespace": None,
        "workload_name": None,
        "workload_type": None,
        "service": None,
        "project": None,
        "service_exec_id": None,
        "allocate_method": None,
        "usage_seconds": None,
        "currency": pool["currency"],
        "source_rows": pool["source_rows"],
        "source_summary_row_hash": None,
        "source_pool_key": pool["source_pool_key"],
        "allocation_version": allocation_version,
        "classification_version": classification_version,
        "weight_model": WEIGHT_MODEL,
        "weight_version": WEIGHT_VERSION,
    }
    if total_weight == 0:
        return (_residual_row(base, amounts, unmatched_count=0, unmatched_weight=Decimal()),)

    output: list[dict[str, Any]] = []
    remaining = dict(amounts)
    all_matched = unmatched_weight == 0
    for index, (key, weight) in enumerate(participants):
        employee_id, author, org, repo, owner, group_id, manager_id = key
        row = dict(base)
        row.update(
            {
                "author": author,
                "org": org,
                "repo": repo,
                "owner": owner,
                "attribution_key": f"employee:{employee_id}",
                "attribution_source": "tencent_ci_build_weighted",
                "attribution_status": "matched",
                "employee_id": employee_id,
                "group_id": group_id,
                "manager_id": manager_id,
                "allocation_weight": weight,
            }
        )
        is_last = index == len(participants) - 1
        for name, amount in amounts.items():
            if amount is None:
                row[name] = None
            elif all_matched and is_last:
                row[name] = remaining[name]
            else:
                row[name] = (amount * weight / total_weight).quantize(
                    _AMOUNT_QUANTUM, rounding=ROUND_HALF_UP
                )
                remaining[name] -= row[name]
        row["dimension_hash"] = _projection_hash(row)
        output.append(row)
    if unmatched_weight > 0 or not participants:
        output.append(
            _residual_row(
                base,
                remaining if participants else amounts,
                unmatched_count=unmatched_count,
                unmatched_weight=unmatched_weight,
            )
        )
    return tuple(output)


def _residual_row(
    base: Mapping[str, Any],
    amounts: Mapping[str, Decimal | None],
    *,
    unmatched_count: int,
    unmatched_weight: Decimal,
) -> dict[str, Any]:
    row = dict(base)
    row.update(
        {
            "author": None,
            "org": None,
            "repo": None,
            "owner": None,
            "attribution_key": "unattributed",
            "attribution_source": "tencent_ci_build_weighted_residual",
            "attribution_status": "unattributed",
            "employee_id": None,
            "group_id": None,
            "manager_id": None,
            "allocation_weight": unmatched_weight,
            "vendor_tags_json": canonical_json(
                {
                    **json.loads(str(base["vendor_tags_json"])),
                    "unmatched_build_count": unmatched_count,
                    "unmatched_weight": _decimal_text(unmatched_weight),
                }
            ),
            **amounts,
        }
    )
    row["dimension_hash"] = _projection_hash(row)
    return row


def _projection_base(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: source.get(key)
        for key in (
            "usage_date",
            "vendor",
            "account_id",
            "service_name",
            "sku_name",
            "usage_type",
            "cost_driver_key",
            "region",
            "org",
            "repo",
            "target_branch",
            "resource_name",
            "vendor_tags_json",
            "namespace",
            "workload_name",
            "workload_type",
            "author",
            "service",
            "project",
            "service_exec_id",
            "allocate_method",
            "usage_seconds",
            "currency",
            *AMOUNT_FIELDS,
        )
    }


def _projection_hash(row: Mapping[str, Any]) -> str:
    keys = (
        "allocation_version", "usage_date", "vendor", "account_id", "currency", "service_name",
        "sku_name", "usage_type", "cost_driver_key", "region", "org", "repo", "target_branch",
        "resource_name", "vendor_tags_json", "source_allocation_scope", "author", "owner",
        "attribution_key", "attribution_source", "attribution_status", "employee_id", "group_id",
        "manager_id", "source_summary_row_hash", "source_pool_key", "classification_version",
        "weight_model", "weight_version", "allocation_weight",
    )
    return content_hash({key: _json_value(row.get(key)) for key in keys})


def _assert_projection_conserved(
    ledger: Iterable[Mapping[str, Any]], projection: Iterable[Mapping[str, Any]]) -> None:
    source = tuple(ledger)
    output = tuple(projection)
    currencies = {str(row.get("currency") or "").upper() for row in (*source, *output)}
    for currency in currencies:
        for name in AMOUNT_FIELDS:
            source_values = [row.get(name) for row in source if str(row.get("currency") or "").upper() == currency]
            output_values = [row.get(name) for row in output if str(row.get("currency") or "").upper() == currency]
            if any(value is None for value in source_values):
                if any(value is not None for value in source_values) or any(value is not None for value in output_values):
                    raise ValueError(f"Tencent {currency} {name} cannot represent mixed known and unknown amounts")
                continue
            if sum((_decimal(value) for value in source_values), Decimal()) != sum(
                (_decimal(value) for value in output_values), Decimal()
            ):
                raise RuntimeError(f"Tencent projection does not conserve {currency} {name}")


# ---- SQL/control-plane helpers -------------------------------------------------


def tencent_allocation_schema_ready(connection: Connection) -> bool:
    return _table_exists(connection, "tencent_billing_import_partition") and _has_column(
        connection, "cost_bq_export_summary_daily", "tencent_cost_class"
    )


def begin_tencent_billing_partition(
    connection: Connection,
    *,
    account_id: str,
    bill_day: date,
    import_generation: str,
) -> None:
    """Fence incomplete Tencent imports from materialization without touching projections."""
    if not tencent_allocation_schema_ready(connection):
        return
    previous = [
        _as_date(value).isoformat()
        for value in connection.execute(
            text(
                """
                SELECT DISTINCT usage_date FROM cost_bq_export_summary_daily
                WHERE vendor='tencent' AND account_id=:account_id AND export_partition_date=:bill_day
                """
            ),
            {"account_id": account_id, "bill_day": bill_day},
        ).scalars()
    ]
    values = {
        "account_id": account_id,
        "bill_day": bill_day,
        "import_generation": import_generation,
        "usage_dates_json": canonical_json(sorted(previous)),
    }
    if connection.dialect.name == "sqlite":
        statement = text(
            """
            INSERT INTO tencent_billing_import_partition (
              account_id, bill_day, import_generation, is_complete, usage_dates_json
            ) VALUES (:account_id, :bill_day, :import_generation, 0, :usage_dates_json)
            ON CONFLICT(account_id, bill_day) DO UPDATE SET
              import_generation=excluded.import_generation, is_complete=0,
              usage_dates_json=excluded.usage_dates_json, updated_at=CURRENT_TIMESTAMP
            """
        )
    else:
        statement = text(
            """
            INSERT INTO tencent_billing_import_partition (
              account_id, bill_day, import_generation, is_complete, usage_dates_json
            ) VALUES (:account_id, :bill_day, :import_generation, 0, CAST(:usage_dates_json AS JSON))
            ON DUPLICATE KEY UPDATE import_generation=VALUES(import_generation), is_complete=0,
              usage_dates_json=VALUES(usage_dates_json), updated_at=CURRENT_TIMESTAMP
            """
        )
    connection.execute(statement, values)


def persist_tencent_billing_metadata(
    connection: Connection,
    *,
    account_id: str,
    bill_day: date,
    import_generation: str,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    """Attach stable codes, current sealed class, and generation after raw page upsert."""
    if not tencent_allocation_schema_ready(connection):
        return
    rule_set = _latest_rule_set(
        connection,
        "tencent_cost_classification_rule_set",
        "classification_version",
        "rules_json",
    )
    rules = normalize_classification_rules(json.loads(rule_set["content"]))
    for row in rows:
        connection.execute(
            text(
                """
                UPDATE cost_bq_export_summary_daily SET
                  tencent_business_code=:business_code,
                  tencent_product_code=:product_code,
                  tencent_component_code=:component_code,
                  tencent_item_code=:item_code,
                  tencent_cost_class=:classification,
                  tencent_classification_version=:classification_version,
                  tencent_import_generation=:import_generation
                WHERE vendor='tencent' AND account_id=:account_id
                  AND export_partition_date=:bill_day AND source_row_hash=:source_row_hash
                """
            ),
            {
                "business_code": row.get("tencent_business_code"),
                "product_code": row.get("tencent_product_code"),
                "component_code": row.get("tencent_component_code"),
                "item_code": row.get("tencent_item_code"),
                "classification": classify_tencent_row(row, rules),
                "classification_version": rule_set["version"],
                "import_generation": import_generation,
                "account_id": account_id,
                "bill_day": bill_day,
                "source_row_hash": row["source_row_hash"],
            },
        )


def complete_tencent_billing_partition(
    connection: Connection,
    *,
    account_id: str,
    bill_day: date,
    import_generation: str,
) -> tuple[date, ...]:
    """Replace one completed raw partition and mark only changed usage dates stale."""
    if not tencent_allocation_schema_ready(connection):
        return ()
    existing = connection.execute(
        text(
            """
            SELECT usage_dates_json FROM tencent_billing_import_partition
            WHERE account_id=:account_id AND bill_day=:bill_day
            """
        ),
        {"account_id": account_id, "bill_day": bill_day},
    ).scalar_one()
    previous = set(json.loads(str(existing)))
    connection.execute(
        text(
            """
            DELETE FROM cost_bq_export_summary_daily
            WHERE vendor='tencent' AND account_id=:account_id AND export_partition_date=:bill_day
              AND COALESCE(tencent_import_generation, '') <> :import_generation
            """
        ),
        {"account_id": account_id, "bill_day": bill_day, "import_generation": import_generation},
    )
    current = {
        _as_date(value).isoformat()
        for value in connection.execute(
            text(
                """
                SELECT DISTINCT usage_date FROM cost_bq_export_summary_daily
                WHERE vendor='tencent' AND account_id=:account_id AND export_partition_date=:bill_day
                """
            ),
            {"account_id": account_id, "bill_day": bill_day},
        ).scalars()
    }
    values = sorted(previous | current)
    partition_rows = tuple(
        connection.execute(
            text(
                """
                SELECT * FROM cost_bq_export_summary_daily
                WHERE vendor='tencent' AND account_id=:account_id AND export_partition_date=:bill_day
                ORDER BY source_row_hash
                """
            ),
            {"account_id": account_id, "bill_day": bill_day},
        ).mappings()
    )
    connection.execute(
        text(
            """
            UPDATE tencent_billing_import_partition
            SET is_complete=1, usage_dates_json=:usage_dates_json,
                source_fingerprint=:source_fingerprint, completed_at=CURRENT_TIMESTAMP
            WHERE account_id=:account_id AND bill_day=:bill_day AND import_generation=:import_generation
            """
        ),
        {
            "account_id": account_id,
            "bill_day": bill_day,
            "import_generation": import_generation,
            "usage_dates_json": canonical_json(values),
            "source_fingerprint": canonical_ledger_fingerprint(partition_rows),
        },
    )
    dates = tuple(date.fromisoformat(value) for value in values)
    _refresh_ledger_state(connection, dates, reason="ledger")
    return dates


def publish_tencent_cost_classification(
    engine: Engine,
    *,
    classification_version: str,
    rules: Any,
    reviewed_by: str | None = None,
) -> int:
    normalized = normalize_classification_rules(rules)
    encoded = canonical_json(list(normalized))
    digest = content_hash(list(normalized))
    with engine.begin() as connection:
        existing = connection.execute(
            text("SELECT content_hash FROM tencent_cost_classification_rule_set WHERE classification_version=:version"),
            {"version": classification_version},
        ).scalar_one_or_none()
        if existing is not None:
            if str(existing) != digest:
                raise ValueError("Tencent classification version is immutable")
            return 0
        existing_content_version = connection.execute(
            text(
                "SELECT classification_version FROM tencent_cost_classification_rule_set "
                "WHERE content_hash=:content_hash"
            ),
            {"content_hash": digest},
        ).scalar_one_or_none()
        if existing_content_version is not None:
            raise ValueError(
                "Tencent classification content is already published as "
                f"{existing_content_version}"
            )
        connection.execute(
            text(
                """
                INSERT INTO tencent_cost_classification_rule_set
                  (classification_version, rules_json, content_hash, reviewed_by)
                VALUES (:version, :rules_json, :content_hash, :reviewed_by)
                """
            ),
            {"version": classification_version, "rules_json": encoded, "content_hash": digest, "reviewed_by": reviewed_by},
        )
        completed = tuple(
            connection.execute(
                text(
                    """
                    SELECT summary.*
                    FROM cost_bq_export_summary_daily summary
                    JOIN tencent_billing_import_partition partition
                      ON partition.account_id = summary.account_id
                     AND partition.bill_day = summary.export_partition_date
                     AND partition.is_complete = 1
                    WHERE summary.vendor = 'tencent' AND summary.account_id = :account_id
                    """
                ),
                {"account_id": ACCOUNT_ID},
            ).mappings()
        )
        changed_dates: set[date] = set()
        for row in completed:
            classification = classify_tencent_row(row, normalized)
            if row.get("tencent_cost_class") != classification:
                connection.execute(
                    _UPDATE_TENCENT_CLASSIFICATION,
                    {
                        "classification": classification,
                        "classification_version": classification_version,
                        "source_row_hash": row["source_row_hash"],
                        "export_partition_date": row["export_partition_date"],
                    },
                )
                changed_dates.add(_as_date(row["usage_date"]))
        _refresh_ledger_state(connection, changed_dates, reason="classification")
        return len(changed_dates)


def refresh_tencent_ci_build_staleness(engine: Engine) -> tuple[date, ...]:
    with engine.begin() as connection:
        dates = {
            _as_date(value)
            for value in connection.execute(
                text("SELECT usage_date FROM tencent_ci_daily_state WHERE account_id=:account_id"),
                {"account_id": ACCOUNT_ID},
            ).scalars()
        }
        dates.update(
            _as_date(value)
            for value in connection.execute(
                text(
                    """
                    SELECT DISTINCT summary.usage_date
                    FROM cost_bq_export_summary_daily summary
                    JOIN tencent_billing_import_partition partition
                      ON partition.account_id=summary.account_id
                     AND partition.bill_day=summary.export_partition_date
                     AND partition.is_complete=1
                    WHERE summary.vendor='tencent' AND summary.account_id=:account_id
                    """
                ),
                {"account_id": ACCOUNT_ID},
            ).scalars()
        )
        dates.update(
            _as_date(value)
            for value in connection.execute(
                text("SELECT usage_date FROM tencent_ci_allocation_publication WHERE account_id=:account_id"),
                {"account_id": ACCOUNT_ID},
            ).scalars()
        )
        fingerprints = _load_build_fingerprints(connection, dates)
        stale: list[date] = []
        for usage_date in sorted(dates):
            fingerprint = fingerprints.get(usage_date, canonical_build_fingerprint(()))
            current = connection.execute(
                text(
                    "SELECT build_fingerprint FROM tencent_ci_daily_state "
                    "WHERE account_id=:account_id AND usage_date=:usage_date"
                ),
                {"account_id": ACCOUNT_ID, "usage_date": usage_date},
            ).scalar_one_or_none()
            if current != fingerprint:
                _upsert_day_state(
                    connection,
                    usage_date=usage_date,
                    build_fingerprint=fingerprint,
                    is_stale=True,
                    stale_reason="build",
                )
                stale.append(usage_date)
        return tuple(stale)


def materialize_tencent_ci_cost_allocation(
    engine: Engine,
    *,
    start_date: date,
    end_date: date,
    allocation_version: str,
) -> TencentAllocationSummary:
    if start_date > end_date:
        raise ValueError("start_date must be before or equal to end_date")
    usage_dates = tuple(_date_range(start_date, end_date))
    with engine.begin() as connection:
        classification = _latest_rule_set(connection, "tencent_cost_classification_rule_set", "classification_version", "rules_json")
        aliases = _latest_rule_set(connection, "tencent_ci_identity_alias_rule_set", "alias_version", "aliases_json")
        manifest = _ensure_manifest(
            connection,
            allocation_version=allocation_version,
            start_date=start_date,
            end_date=end_date,
            classification=classification,
            aliases=aliases,
        )
        roster = json.loads(str(manifest["roster_json"]))
        aliases_map = {str(key).lower(): int(value) for key, value in json.loads(str(aliases["content"])).items()}
        builds_by_date = _load_builds_by_date(connection, usage_dates)
    build_fingerprints = {
        usage_date: canonical_build_fingerprint(builds)
        for usage_date, builds in builds_by_date.items()
    }
    days_validated = 0
    written = 0
    for usage_date in usage_dates:
        failure: dict[str, Any] | None = None
        try:
            with engine.begin() as connection:
                ledger = _load_complete_ledger(connection, usage_date)
                if not ledger:
                    continue
                ledger_fingerprint = canonical_ledger_fingerprint(ledger)
                build_fingerprint = build_fingerprints[usage_date]
                existing = connection.execute(
                    _SELECT_ALLOCATION_DAY,
                    {"allocation_version": allocation_version, "usage_date": usage_date},
                ).mappings().first()
                if existing is not None and (
                    existing["ledger_fingerprint"] != ledger_fingerprint
                    or existing["build_fingerprint"] != build_fingerprint
                ):
                    raise ValueError("An allocation version cannot be reused with changed daily inputs")
                if existing is not None and existing["status"] == "published":
                    raise ValueError("A published Tencent allocation version is immutable")
                try:
                    projection, pool_fingerprint, statistics = build_tencent_projection(
                        ledger_rows=ledger,
                        builds=builds_by_date[usage_date],
                        roster=roster,
                        aliases=aliases_map,
                        allocation_version=allocation_version,
                        classification_version=str(classification["version"]),
                    )
                except Exception as exc:
                    failure = {
                        "allocation_version": allocation_version,
                        "usage_date": usage_date,
                        "status": "failed",
                        "ledger_fingerprint": ledger_fingerprint,
                        "build_fingerprint": build_fingerprint,
                        "pool_fingerprint": content_hash([]),
                        "projection_row_count": 0,
                        "statistics": {"error": str(exc)},
                        "failure_reason": str(exc),
                    }
                    raise
                connection.execute(
                    _DELETE_STAGED_PROJECTION,
                    {"allocation_version": allocation_version, "usage_date": usage_date},
                )
                _write_projection(connection, projection)
                _upsert_allocation_day(
                    connection,
                    allocation_version=allocation_version,
                    usage_date=usage_date,
                    status="validated",
                    ledger_fingerprint=ledger_fingerprint,
                    build_fingerprint=build_fingerprint,
                    pool_fingerprint=pool_fingerprint,
                    projection_row_count=len(projection),
                    statistics=statistics,
                    failure_reason=None,
                )
                _upsert_day_state(
                    connection,
                    usage_date=usage_date,
                    ledger_fingerprint=ledger_fingerprint,
                    build_fingerprint=build_fingerprint,
                    is_stale=True,
                    stale_reason="staged",
                )
        except Exception:
            if failure is not None:
                with engine.begin() as connection:
                    _upsert_allocation_day(connection, **failure)
            raise
        days_validated += 1
        written += len(projection)
    return TencentAllocationSummary(allocation_version, start_date, end_date, days_validated, written)


def publish_tencent_ci_cost_allocation(
    engine: Engine,
    *,
    allocation_version: str,
    usage_dates: Iterable[date],
    rollback: bool = False,
) -> TencentPublicationSummary:
    selected = tuple(sorted(set(usage_dates)))
    if not selected:
        raise ValueError("at least one Tencent usage date is required")
    published = 0
    for usage_date in selected:
        published += _publish_tencent_day(
            engine,
            allocation_version=allocation_version,
            usage_date=usage_date,
            rollback=rollback,
        )
    return TencentPublicationSummary(allocation_version, selected, published, rollback)


def _publish_tencent_day(
    engine: Engine,
    *,
    allocation_version: str,
    usage_date: date,
    rollback: bool,
) -> int:
    with engine.begin() as connection:
        day = connection.execute(
            _locked_statement(connection, _SELECT_ALLOCATION_DAY),
            {"allocation_version": allocation_version, "usage_date": usage_date},
        ).mappings().first()
        if day is None or day["status"] not in {"validated", "published"}:
            raise ValueError("Tencent allocation day must be validated before publication")
        pointer = connection.execute(
            _locked_statement(connection, _SELECT_POINTER),
            {"usage_date": usage_date},
        ).mappings().first()
        ledger = _load_complete_ledger(connection, usage_date)
        current_ledger = canonical_ledger_fingerprint(ledger)
        current_build = _load_build_fingerprints(connection, (usage_date,)).get(
            usage_date, canonical_build_fingerprint(())
        )
        inputs_match = (
            current_ledger == day["ledger_fingerprint"] and current_build == day["build_fingerprint"]
        )
        if not inputs_match and not rollback:
            _upsert_day_state(
                connection,
                usage_date=usage_date,
                ledger_fingerprint=current_ledger,
                build_fingerprint=current_build,
                is_stale=True,
                stale_reason="publish_input_changed",
            )
            raise ValueError("Tencent allocation input changed; materialize a new version before publish")
        if pointer is not None and pointer["active_allocation_version"] == allocation_version:
            return 0
        connection.execute(
            _DELETE_PUBLISHED_TENCENT_PROJECTION,
            {"usage_date": usage_date},
        )
        connection.execute(
            _INSERT_PUBLISHED_PROJECTION,
            {"allocation_version": allocation_version, "usage_date": usage_date},
        )
        _upsert_pointer(
            connection,
            usage_date=usage_date,
            allocation_version=allocation_version,
            ledger_fingerprint=current_ledger,
            build_fingerprint=current_build,
        )
        connection.execute(
            _INSERT_PUBLICATION_EVENT,
            {
                "usage_date": usage_date,
                "allocation_version": allocation_version,
                "event_type": "rollback" if rollback else "publish",
                "previous_allocation_version": pointer["active_allocation_version"] if pointer else None,
            },
        )
        connection.execute(
            text(
                "UPDATE tencent_ci_allocation_day SET status='published', published_at=CURRENT_TIMESTAMP "
                "WHERE allocation_version=:allocation_version AND usage_date=:usage_date"
            ),
            {"allocation_version": allocation_version, "usage_date": usage_date},
        )
        _upsert_day_state(
            connection,
            usage_date=usage_date,
            ledger_fingerprint=current_ledger,
            build_fingerprint=current_build,
            active_allocation_version=allocation_version,
            is_stale=not inputs_match,
            stale_reason="rollback_input_changed" if rollback and not inputs_match else None,
        )
        return 1


def _table_exists(connection: Connection, table: str) -> bool:
    if connection.dialect.name == "sqlite":
        return connection.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:table"),
            {"table": table},
        ).first() is not None
    return connection.execute(
        text(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema=DATABASE() AND table_name=:table LIMIT 1
            """
        ),
        {"table": table},
    ).first() is not None


def _has_column(connection: Connection, table: str, column: str) -> bool:
    if connection.dialect.name == "sqlite":
        return any(str(row[1]) == column for row in connection.execute(text(f"PRAGMA table_info({table})")))
    return connection.execute(
        text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema=DATABASE() AND table_name=:table AND column_name=:column LIMIT 1
            """
        ),
        {"table": table, "column": column},
    ).first() is not None


def _latest_rule_set(connection: Connection, table: str, version_column: str, content_column: str) -> dict[str, str]:
    row = connection.execute(
        text(
            f"SELECT {version_column} AS version, {content_column} AS content, content_hash "
            f"FROM {table} ORDER BY published_at DESC, {version_column} DESC LIMIT 1"
        )
    ).mappings().first()
    if row is None:
        raise ValueError(f"No Tencent rule set is published in {table}")
    content = row["content"]
    if not isinstance(content, str):
        content = canonical_json(content)
    return {"version": str(row["version"]), "content": str(content), "content_hash": str(row["content_hash"])}


def _ensure_manifest(
    connection: Connection,
    *,
    allocation_version: str,
    start_date: date,
    end_date: date,
    classification: Mapping[str, str],
    aliases: Mapping[str, str],
) -> Mapping[str, Any]:
    existing = connection.execute(
        text("SELECT * FROM tencent_ci_allocation_manifest WHERE allocation_version=:allocation_version"),
        {"allocation_version": allocation_version},
    ).mappings().first()
    if existing is not None:
        expected = {
            "start_date": start_date,
            "end_date": end_date,
            "classification_version": classification["version"],
            "classification_hash": classification["content_hash"],
            "alias_version": aliases["version"],
            "alias_hash": aliases["content_hash"],
            "weight_model": WEIGHT_MODEL,
            "weight_version": WEIGHT_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
        }
        if any(str(existing[key]) != str(value) for key, value in expected.items()):
            raise ValueError("Tencent allocation manifest is immutable")
        snapshot = connection.execute(
            text("SELECT roster_json FROM tencent_ci_roster_snapshot WHERE snapshot_id=:snapshot_id"),
            {"snapshot_id": existing["roster_snapshot_id"]},
        ).mappings().one()
        return {"roster_json": snapshot["roster_json"]}
    roster_rows = tuple(
        connection.execute(
            text(
                """
                SELECT employee.id AS employee_id, employee.email, employee.github_id, employee.en_name,
                       employee.group_id, employee.manager_id
                FROM roster_employees employee
                JOIN roster_groups roster_group
                  ON roster_group.id = employee.group_id AND roster_group.is_active = 1
                WHERE employee.is_active = 1
                ORDER BY employee.id
                """
            )
        ).mappings()
    )
    snapshot_id, roster_json = snapshot_roster(roster_rows)
    connection.execute(
        _insert_ignore(connection, "tencent_ci_roster_snapshot", "snapshot_id, content_hash, roster_json, resolved_at", ":snapshot_id, :content_hash, :roster_json, CURRENT_TIMESTAMP", "snapshot_id"),
        {"snapshot_id": snapshot_id, "content_hash": snapshot_id, "roster_json": roster_json},
    )
    connection.execute(
        text(
            """
            INSERT INTO tencent_ci_allocation_manifest (
              allocation_version, account_id, start_date, end_date, classification_version,
              classification_hash, alias_version, alias_hash, roster_snapshot_id, weight_model,
              weight_version, algorithm_version
            ) VALUES (
              :allocation_version, :account_id, :start_date, :end_date, :classification_version,
              :classification_hash, :alias_version, :alias_hash, :roster_snapshot_id, :weight_model,
              :weight_version, :algorithm_version
            )
            """
        ),
        {
            "allocation_version": allocation_version,
            "account_id": ACCOUNT_ID,
            "start_date": start_date,
            "end_date": end_date,
            "classification_version": classification["version"],
            "classification_hash": classification["content_hash"],
            "alias_version": aliases["version"],
            "alias_hash": aliases["content_hash"],
            "roster_snapshot_id": snapshot_id,
            "weight_model": WEIGHT_MODEL,
            "weight_version": WEIGHT_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
        },
    )
    return {"roster_json": roster_json}


def _load_complete_ledger(connection: Connection, usage_date: date) -> tuple[dict[str, Any], ...]:
    incomplete = connection.execute(
        text(
            """
            SELECT 1 FROM cost_bq_export_summary_daily summary
            LEFT JOIN tencent_billing_import_partition partition
              ON partition.account_id = summary.account_id
             AND partition.bill_day = summary.export_partition_date
            WHERE summary.vendor = 'tencent' AND summary.account_id = :account_id
              AND summary.usage_date = :usage_date
              AND (partition.account_id IS NULL OR partition.is_complete <> 1)
            LIMIT 1
            """
        ),
        {"account_id": ACCOUNT_ID, "usage_date": usage_date},
    ).first()
    if incomplete is not None:
        raise ValueError("Tencent usage date has an incomplete source partition")
    rows = connection.execute(
        text(
            """
            SELECT summary.*
            FROM cost_bq_export_summary_daily summary
            JOIN tencent_billing_import_partition partition
              ON partition.account_id = summary.account_id
             AND partition.bill_day = summary.export_partition_date
             AND partition.is_complete = 1
            WHERE summary.vendor = 'tencent' AND summary.account_id = :account_id
              AND summary.usage_date = :usage_date
            ORDER BY summary.source_row_hash
            """
        ),
        {"account_id": ACCOUNT_ID, "usage_date": usage_date},
    ).mappings()
    return tuple(dict(row) for row in rows)


def _load_builds_by_date(
    connection: Connection, dates: Iterable[date]
) -> dict[date, tuple[dict[str, Any], ...]]:
    requested = tuple(sorted(set(dates)))
    if not requested:
        return {}
    lower = datetime.combine(requested[0], time.min, tzinfo=_BEIJING).astimezone(UTC).replace(tzinfo=None)
    upper = datetime.combine(
        date.fromordinal(requested[-1].toordinal() + 1), time.min, tzinfo=_BEIJING
    ).astimezone(UTC).replace(tzinfo=None)
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    rows = connection.execute(
        text(
            """
            SELECT source_prow_job_id, start_time, completion_time, cloud_phase, author, org, repo,
                   job_name, run_seconds, total_seconds
            FROM ci_l1_builds
            WHERE cloud_phase = 'TENCENT' AND completion_time IS NOT NULL
              AND start_time >= :lower AND start_time < :upper
            ORDER BY start_time, source_prow_job_id
            """
        ),
        {"lower": lower, "upper": upper},
    ).mappings()
    requested_set = set(requested)
    for row in rows:
        usage_date = beijing_usage_date(row["start_time"])
        if usage_date in requested_set:
            grouped[usage_date].append({**dict(row), "participates": True})
    return {usage_date: tuple(grouped.get(usage_date, ())) for usage_date in requested}


def _load_build_fingerprints(connection: Connection, dates: Iterable[date]) -> dict[date, str]:
    return {
        usage_date: canonical_build_fingerprint(builds)
        for usage_date, builds in _load_builds_by_date(connection, dates).items()
    }


def _refresh_ledger_state(connection: Connection, dates: Iterable[date], *, reason: str) -> None:
    for usage_date in sorted(set(dates)):
        fingerprint = canonical_ledger_fingerprint(_load_complete_ledger(connection, usage_date))
        previous = connection.execute(
            text(
                """
                SELECT ledger_fingerprint FROM tencent_ci_daily_state
                WHERE account_id=:account_id AND usage_date=:usage_date
                """
            ),
            {"account_id": ACCOUNT_ID, "usage_date": usage_date},
        ).scalar_one_or_none()
        if previous != fingerprint:
            _upsert_day_state(
                connection,
                usage_date=usage_date,
                ledger_fingerprint=fingerprint,
                is_stale=True,
                stale_reason=reason,
            )


def _upsert_day_state(
    connection: Connection,
    *,
    usage_date: date,
    ledger_fingerprint: str | None = None,
    build_fingerprint: str | None = None,
    active_allocation_version: str | None = None,
    is_stale: bool,
    stale_reason: str | None,
) -> None:
    values = {
        "account_id": ACCOUNT_ID,
        "usage_date": usage_date,
        "ledger_fingerprint": ledger_fingerprint,
        "build_fingerprint": build_fingerprint,
        "active_allocation_version": active_allocation_version,
        "is_stale": int(is_stale),
        "stale_reason": stale_reason,
    }
    if connection.dialect.name == "sqlite":
        statement = text(
            """
            INSERT INTO tencent_ci_daily_state (
              account_id, usage_date, ledger_fingerprint, build_fingerprint,
              active_allocation_version, is_stale, stale_reason
            ) VALUES (
              :account_id, :usage_date, :ledger_fingerprint, :build_fingerprint,
              :active_allocation_version, :is_stale, :stale_reason
            ) ON CONFLICT(account_id, usage_date) DO UPDATE SET
              ledger_fingerprint=COALESCE(excluded.ledger_fingerprint, tencent_ci_daily_state.ledger_fingerprint),
              build_fingerprint=COALESCE(excluded.build_fingerprint, tencent_ci_daily_state.build_fingerprint),
              active_allocation_version=COALESCE(excluded.active_allocation_version, tencent_ci_daily_state.active_allocation_version),
              is_stale=excluded.is_stale, stale_reason=excluded.stale_reason, updated_at=CURRENT_TIMESTAMP
            """
        )
    else:
        statement = text(
            """
            INSERT INTO tencent_ci_daily_state (
              account_id, usage_date, ledger_fingerprint, build_fingerprint,
              active_allocation_version, is_stale, stale_reason
            ) VALUES (
              :account_id, :usage_date, :ledger_fingerprint, :build_fingerprint,
              :active_allocation_version, :is_stale, :stale_reason
            ) ON DUPLICATE KEY UPDATE
              ledger_fingerprint=COALESCE(VALUES(ledger_fingerprint), ledger_fingerprint),
              build_fingerprint=COALESCE(VALUES(build_fingerprint), build_fingerprint),
              active_allocation_version=COALESCE(VALUES(active_allocation_version), active_allocation_version),
              is_stale=VALUES(is_stale), stale_reason=VALUES(stale_reason), updated_at=CURRENT_TIMESTAMP
            """
        )
    connection.execute(statement, values)


def _upsert_allocation_day(connection: Connection, **values: Any) -> None:
    values["statistics_json"] = canonical_json(values["statistics"])
    if connection.dialect.name == "sqlite":
        statement = text(
            """
            INSERT INTO tencent_ci_allocation_day (
              allocation_version, usage_date, status, ledger_fingerprint, build_fingerprint,
              pool_fingerprint, projection_row_count, statistics_json, validated_at, failure_reason
            ) VALUES (
              :allocation_version, :usage_date, :status, :ledger_fingerprint, :build_fingerprint,
              :pool_fingerprint, :projection_row_count, :statistics_json,
              CASE WHEN :status='validated' THEN CURRENT_TIMESTAMP ELSE NULL END, :failure_reason
            ) ON CONFLICT(allocation_version, usage_date) DO UPDATE SET
              status=excluded.status, ledger_fingerprint=excluded.ledger_fingerprint,
              build_fingerprint=excluded.build_fingerprint, pool_fingerprint=excluded.pool_fingerprint,
              projection_row_count=excluded.projection_row_count, statistics_json=excluded.statistics_json,
              validated_at=excluded.validated_at, failure_reason=excluded.failure_reason
            """
        )
    else:
        statement = text(
            """
            INSERT INTO tencent_ci_allocation_day (
              allocation_version, usage_date, status, ledger_fingerprint, build_fingerprint,
              pool_fingerprint, projection_row_count, statistics_json, validated_at, failure_reason
            ) VALUES (
              :allocation_version, :usage_date, :status, :ledger_fingerprint, :build_fingerprint,
              :pool_fingerprint, :projection_row_count, CAST(:statistics_json AS JSON),
              IF(:status='validated', CURRENT_TIMESTAMP, NULL), :failure_reason
            ) ON DUPLICATE KEY UPDATE
              status=VALUES(status), ledger_fingerprint=VALUES(ledger_fingerprint),
              build_fingerprint=VALUES(build_fingerprint), pool_fingerprint=VALUES(pool_fingerprint),
              projection_row_count=VALUES(projection_row_count), statistics_json=VALUES(statistics_json),
              validated_at=VALUES(validated_at), failure_reason=VALUES(failure_reason)
            """
        )
    connection.execute(statement, values)


def _write_projection(connection: Connection, rows: Iterable[Mapping[str, Any]]) -> None:
    values = tuple(rows)
    if not values:
        raise ValueError("Tencent allocation projection cannot be empty")
    bound = bind_decimal_rows([dict(row) for row in values]) if connection.dialect.name == "sqlite" else values
    connection.execute(_INSERT_STAGED_PROJECTION, bound)


def _upsert_pointer(connection: Connection, *, usage_date: date, allocation_version: str, ledger_fingerprint: str, build_fingerprint: str) -> None:
    values = {"usage_date": usage_date, "allocation_version": allocation_version, "ledger_fingerprint": ledger_fingerprint, "build_fingerprint": build_fingerprint}
    if connection.dialect.name == "sqlite":
        statement = text(
            """
            INSERT INTO tencent_ci_allocation_publication (
              vendor, account_id, usage_date, active_allocation_version, ledger_fingerprint, build_fingerprint
            ) VALUES ('tencent', :account_id, :usage_date, :allocation_version, :ledger_fingerprint, :build_fingerprint)
            ON CONFLICT(vendor, account_id, usage_date) DO UPDATE SET
              active_allocation_version=excluded.active_allocation_version,
              ledger_fingerprint=excluded.ledger_fingerprint, build_fingerprint=excluded.build_fingerprint,
              published_at=CURRENT_TIMESTAMP
            """
        )
    else:
        statement = text(
            """
            INSERT INTO tencent_ci_allocation_publication (
              vendor, account_id, usage_date, active_allocation_version, ledger_fingerprint, build_fingerprint
            ) VALUES ('tencent', :account_id, :usage_date, :allocation_version, :ledger_fingerprint, :build_fingerprint)
            ON DUPLICATE KEY UPDATE active_allocation_version=VALUES(active_allocation_version),
              ledger_fingerprint=VALUES(ledger_fingerprint), build_fingerprint=VALUES(build_fingerprint),
              published_at=CURRENT_TIMESTAMP
            """
        )
    connection.execute(statement, {"account_id": ACCOUNT_ID, **values})


def _insert_ignore(connection: Connection, table: str, columns: str, values: str, key: str):
    if connection.dialect.name == "sqlite":
        return text(f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({values})")
    return text(f"INSERT IGNORE INTO {table} ({columns}) VALUES ({values})")


def _locked_statement(connection: Connection, statement):
    return statement if connection.dialect.name == "sqlite" else text(f"{statement.text} FOR UPDATE")


def _identity_matches(
    rows: Iterable[Mapping[str, Any]], field: str, identity: str
) -> tuple[Mapping[str, Any], ...]:
    matches = {
        int(row["employee_id"]): row
        for row in rows
        if _text_or_none(row.get(field)) is not None and str(row[field]).lower() == identity.lower()
    }
    return tuple(matches.values())


def _resolved(row: Mapping[str, Any], path: str) -> ResolvedIdentity:
    return ResolvedIdentity(
        int(row["employee_id"]),
        _text_or_none(row.get("email")),
        int(row["group_id"]),
        int(row["manager_id"]) if row.get("manager_id") is not None else None,
        path,
    )


def _sum_optional_amount(rows: Iterable[Mapping[str, Any]], field: str) -> Decimal | None:
    values = [row.get(field) for row in rows]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError(f"Tencent {field} pool mixes known and unknown amounts")
    return sum((_decimal(value) for value in values), Decimal())


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value if value not in (None, "") else 0))


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _nullable_decimal_text(value: Any) -> str | None:
    return None if value is None else _decimal_text(_decimal(value))


def _text_or_none(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value or None


def _email_local(value: Any) -> str:
    return str(value or "").split("@", 1)[0]


def _normalized_identity(value: Any) -> str:
    return "".join(character for character in _email_local(value).lower() if character.isalnum())


def _time_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()
    return str(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _date_range(start_date: date, end_date: date):
    current = start_date
    while current <= end_date:
        yield current
        current = date.fromordinal(current.toordinal() + 1)


_UPDATE_TENCENT_CLASSIFICATION = text(
    """
    UPDATE cost_bq_export_summary_daily
    SET tencent_cost_class=:classification, tencent_classification_version=:classification_version
    WHERE vendor='tencent' AND account_id='100050658403'
      AND source_row_hash=:source_row_hash AND export_partition_date=:export_partition_date
    """
)
_SELECT_ALLOCATION_DAY = text(
    """
    SELECT * FROM tencent_ci_allocation_day
    WHERE allocation_version=:allocation_version AND usage_date=:usage_date
    """
)
_SELECT_POINTER = text(
    """
    SELECT * FROM tencent_ci_allocation_publication
    WHERE vendor='tencent' AND account_id='100050658403' AND usage_date=:usage_date
    """
)
_DELETE_STAGED_PROJECTION = text(
    """
    DELETE FROM tencent_ci_allocation_projection
    WHERE allocation_version=:allocation_version AND usage_date=:usage_date
    """
)
_DELETE_PUBLISHED_TENCENT_PROJECTION = text(
    """
    DELETE FROM cost_attribution_daily
    WHERE vendor='tencent' AND account_id='100050658403' AND usage_date=:usage_date
    """
)
_INSERT_PUBLICATION_EVENT = text(
    """
    INSERT INTO tencent_ci_allocation_publication_event (
      vendor, account_id, usage_date, allocation_version, event_type, previous_allocation_version
    ) VALUES ('tencent', '100050658403', :usage_date, :allocation_version, :event_type, :previous_allocation_version)
    """
)

_PROJECTION_COLUMNS = """
  allocation_version, usage_date, vendor, account_id, service_name, sku_name, usage_type,
  cost_driver_key, region, org, repo, target_branch, resource_name, vendor_tags_json,
  source_allocation_scope, namespace, workload_name, workload_type, author, owner, service,
  project, service_exec_id, attribution_key, attribution_source, attribution_status,
  allocate_method, employee_id, group_id, manager_id, usage_seconds, list_cost,
  effective_cost, credit_amount, net_cost, currency, source_rows, source_summary_row_hash,
  classification_version, weight_model, weight_version, allocation_weight, source_pool_key,
  dimension_hash
"""
_INSERT_STAGED_PROJECTION = text(
    f"""
    INSERT INTO tencent_ci_allocation_projection ({_PROJECTION_COLUMNS}) VALUES (
      :allocation_version, :usage_date, :vendor, :account_id, :service_name, :sku_name, :usage_type,
      :cost_driver_key, :region, :org, :repo, :target_branch, :resource_name, :vendor_tags_json,
      :source_allocation_scope, :namespace, :workload_name, :workload_type, :author, :owner, :service,
      :project, :service_exec_id, :attribution_key, :attribution_source, :attribution_status,
      :allocate_method, :employee_id, :group_id, :manager_id, :usage_seconds, :list_cost,
      :effective_cost, :credit_amount, :net_cost, :currency, :source_rows, :source_summary_row_hash,
      :classification_version, :weight_model, :weight_version, :allocation_weight, :source_pool_key,
      :dimension_hash
    )
    """
)
_INSERT_PUBLISHED_PROJECTION = text(
    """
    INSERT INTO cost_attribution_daily (
      usage_date, vendor, account_id, service_name, sku_name, usage_type, cost_driver_key, region,
      org, repo, target_branch, resource_name, vendor_tags_json, source_allocation_scope, namespace,
      workload_name, workload_type, author, owner, service, project, service_exec_id, attribution_key,
      attribution_source, attribution_status, allocate_method, employee_id, group_id, manager_id,
      usage_seconds, list_cost, effective_cost, credit_amount, net_cost, currency, source_rows,
      source_summary_row_hash, allocation_version, classification_version, weight_model, weight_version,
      allocation_weight, source_pool_key, dimension_hash
    ) SELECT
      usage_date, vendor, account_id, service_name, sku_name, usage_type, cost_driver_key, region,
      org, repo, target_branch, resource_name, vendor_tags_json, source_allocation_scope, namespace,
      workload_name, workload_type, author, owner, service, project, service_exec_id, attribution_key,
      attribution_source, attribution_status, allocate_method, employee_id, group_id, manager_id,
      usage_seconds, list_cost, effective_cost, credit_amount, net_cost, currency, source_rows,
      source_summary_row_hash, allocation_version, classification_version, weight_model, weight_version,
      allocation_weight, source_pool_key, dimension_hash
    FROM tencent_ci_allocation_projection
    WHERE allocation_version=:allocation_version AND usage_date=:usage_date
    """
)
