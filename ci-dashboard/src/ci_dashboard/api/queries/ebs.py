from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping
import json

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ci_dashboard.api.queries.base import CommonFilters, to_number

UNATTACHED_BLOCK_VOLUME_LIMIT = 50
UNATTACHED_BLOCK_VOLUME_SYNC_JOB = "sync-unattached-block-volumes"
UNATTACHED_EBS_VOLUME_LIMIT = UNATTACHED_BLOCK_VOLUME_LIMIT
UNATTACHED_EBS_SYNC_JOB = "sync-unattached-ebs-volumes"


def get_unattached_block_volumes(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    with engine.begin() as connection:
        latest_snapshot_date = _latest_block_volume_snapshot_date(connection, filters)

        latest_source_where_clause, latest_source_params = _build_block_volume_source_where(
            filters,
            table_alias="s",
        )
        volume_where_clause, volume_params = _build_block_volume_source_where(
            filters,
            table_alias="v",
        )
        billing_where_clause, billing_params = _build_block_volume_billing_where(filters)
        resource_names_cte = _block_volume_resource_names_cte(connection)
        rows = connection.execute(
            text(
                f"""
                WITH latest_snapshots AS (
                  SELECT
                    s.vendor AS vendor,
                    s.account_id AS account_id,
                    MAX(s.snapshot_date) AS snapshot_date
                  FROM cost_unattached_block_volume_daily s
                  WHERE {latest_source_where_clause}
                  GROUP BY s.vendor, s.account_id
                ),
                available_volumes AS (
                  SELECT v.*
                  FROM cost_unattached_block_volume_daily v
                  JOIN latest_snapshots latest
                    ON latest.vendor = v.vendor
                   AND latest.account_id = v.account_id
                   AND latest.snapshot_date = v.snapshot_date
                  WHERE LOWER(COALESCE(v.state, '')) NOT IN ('in-use', 'attached')
                    AND {volume_where_clause}
                ),
                volume_resource_names AS (
                  {resource_names_cte}
                ),
                volume_costs AS (
                  SELECT
                    v.vendor AS vendor,
                    v.account_id AS account_id,
                    v.region AS region,
                    v.volume_id AS volume_id,
                    MAX(v.availability_zone) AS availability_zone,
                    MAX(v.size_gib) AS size_gib,
                    MAX(CAST(v.tags_json AS CHAR)) AS tags_json,
                    MAX(v.owner) AS tagged_owner,
                    MAX(v.first_seen_available) AS first_seen_available,
                    MAX(v.source_created_at) AS source_created_at,
                    SUM(COALESCE(c.net_cost, 0)) AS cost,
                    COUNT(c.resource_name) AS matched_cost_rows,
                    MAX(c.owner) AS billing_owner,
                    MAX(c.author) AS billing_author
                  FROM available_volumes v
                  LEFT JOIN volume_resource_names rn
                    ON rn.vendor = v.vendor
                   AND rn.account_id = v.account_id
                   AND rn.region = v.region
                   AND rn.volume_id = v.volume_id
                  LEFT JOIN cost_attribution_daily c
                    ON c.vendor = v.vendor
                   AND c.account_id = v.account_id
                   AND c.resource_name = rn.resource_name
                   AND c.usage_date >= v.first_seen_available
                   AND {billing_where_clause}
                  GROUP BY v.vendor, v.account_id, v.region, v.volume_id
                ),
                volume_rows AS (
                  SELECT
                    vc.*,
                    COALESCE(
                      NULLIF(vc.tagged_owner, ''),
                      NULLIF(vc.billing_owner, ''),
                      NULLIF(vc.billing_author, '')
                    ) AS resolved_owner
                  FROM volume_costs vc
                ),
                owner_matches AS (
                  SELECT
                    vr.vendor AS vendor,
                    vr.account_id AS account_id,
                    vr.region AS region,
                    vr.volume_id AS volume_id,
                    e.id AS employee_id,
                    ROW_NUMBER() OVER (
                      PARTITION BY vr.vendor, vr.account_id, vr.region, vr.volume_id
                      ORDER BY
                        CASE WHEN LOWER(e.email) = LOWER(vr.resolved_owner) THEN 0 ELSE 1 END,
                        CASE WHEN e.is_active = 1 THEN 0 ELSE 1 END,
                        e.id
                    ) AS match_rank
                  FROM volume_rows vr
                  JOIN roster_employees e
                    ON (
                      LOWER(e.email) = LOWER(vr.resolved_owner)
                      OR LOWER(e.github_id) = LOWER(vr.resolved_owner)
                    )
                  WHERE TRIM(COALESCE(vr.resolved_owner, '')) <> ''
                )
                SELECT
                  vr.vendor AS vendor,
                  vr.volume_id AS volume_id,
                  vr.tags_json AS tags_json,
                  vr.resolved_owner AS owner,
                  CASE
                    WHEN TRIM(COALESCE(vr.resolved_owner, '')) = '' THEN 'missing'
                    WHEN owner_employee.id IS NULL THEN 'missing'
                    WHEN owner_employee.is_active = 1 THEN 'active'
                    ELSE 'inactive'
                  END AS owner_status,
                  COALESCE(team_group.name, '') AS team,
                  COALESCE(manager.name, '') AS manager,
                  vr.size_gib AS size_gib,
                  vr.cost AS cost,
                  vr.matched_cost_rows AS matched_cost_rows,
                  vr.first_seen_available AS first_seen_available,
                  vr.source_created_at AS source_created_at
                FROM volume_rows vr
                LEFT JOIN owner_matches owner_match
                  ON owner_match.vendor = vr.vendor
                 AND owner_match.account_id = vr.account_id
                 AND owner_match.region = vr.region
                 AND owner_match.volume_id = vr.volume_id
                 AND owner_match.match_rank = 1
                LEFT JOIN roster_employees owner_employee
                  ON owner_employee.id = owner_match.employee_id
                LEFT JOIN roster_groups team_group
                  ON team_group.id = owner_employee.group_id
                LEFT JOIN roster_employees manager
                  ON manager.id = owner_employee.manager_id
                ORDER BY
                  CASE WHEN vr.matched_cost_rows > 0 THEN 0 ELSE 1 END,
                  vr.cost DESC,
                  vr.first_seen_available ASC,
                  vr.vendor,
                  vr.volume_id
                LIMIT :limit
                """
            ),
            {
                **billing_params,
                **latest_source_params,
                **volume_params,
                "limit": UNATTACHED_BLOCK_VOLUME_LIMIT,
            },
        ).mappings()
        items = [_unattached_block_volume_item(row) for row in rows]

    return {
        "items": items,
        "meta": {
            **filters.meta(),
            "latest_snapshot_date": _date_text(latest_snapshot_date),
            "limit": UNATTACHED_BLOCK_VOLUME_LIMIT,
            "cost_basis": "net_cost",
            "resource_type": "block_volume",
        },
    }


def get_unattached_ebs_volumes(engine: Engine, filters: CommonFilters) -> dict[str, Any]:
    return get_unattached_block_volumes(engine, filters)


def _latest_block_volume_snapshot_date(
    connection: Connection,
    filters: CommonFilters,
) -> date | None:
    source_where_clause, source_params = _build_block_volume_source_where(filters)
    row = connection.execute(
        text(
            f"""
            SELECT MAX(snapshot_date) AS latest_snapshot_date
            FROM cost_unattached_block_volume_daily
            WHERE {source_where_clause}
            """
        ),
        source_params,
    ).mappings().first()
    if row is None:
        return None
    return _parse_date(row["latest_snapshot_date"])


def _build_block_volume_source_where(
    filters: CommonFilters,
    *,
    table_alias: str = "",
) -> tuple[str, dict[str, Any]]:
    prefix = f"{table_alias}." if table_alias else ""
    conditions = ["1=1"]
    params: dict[str, Any] = {}
    if filters.cost_vendor:
        conditions.append(f"{prefix}vendor = :volume_cost_vendor")
        params["volume_cost_vendor"] = filters.cost_vendor
    if filters.cost_account_id:
        conditions.append(f"{prefix}account_id = :volume_cost_account_id")
        params["volume_cost_account_id"] = filters.cost_account_id
    return " AND ".join(conditions), params


def _build_block_volume_billing_where(filters: CommonFilters) -> tuple[str, dict[str, Any]]:
    conditions = ["1=1"]
    params: dict[str, Any] = {}
    if filters.start_date:
        conditions.append("c.usage_date >= :usage_date_from")
        params["usage_date_from"] = filters.start_date
    if filters.end_date:
        conditions.append("c.usage_date <= :usage_date_to")
        params["usage_date_to"] = filters.end_date
    return " AND ".join(conditions), params


def _block_volume_resource_names_cte(connection: Connection) -> str:
    if connection.dialect.name == "sqlite":
        return """
          SELECT
            v.vendor,
            v.account_id,
            v.region,
            v.volume_id,
            v.volume_id AS resource_name
          FROM available_volumes v
          UNION ALL
          SELECT
            v.vendor,
            v.account_id,
            v.region,
            v.volume_id,
            'volume/' || v.volume_id AS resource_name
          FROM available_volumes v
          WHERE v.vendor = 'aws'
          UNION ALL
          SELECT
            v.vendor,
            v.account_id,
            v.region,
            v.volume_id,
            'arn' || char(58) || 'aws' || char(58) || 'ec2' || char(58)
              || v.region || char(58) || v.account_id || char(58)
              || 'volume/' || v.volume_id AS resource_name
          FROM available_volumes v
          WHERE v.vendor = 'aws'
          UNION ALL
          SELECT
            v.vendor,
            v.account_id,
            v.region,
            v.volume_id,
            'projects/' || v.account_id || '/zones/' || v.availability_zone
              || '/disks/' || v.volume_id AS resource_name
          FROM available_volumes v
          WHERE v.vendor = 'gcp'
            AND COALESCE(v.availability_zone, '') <> ''
          UNION ALL
          SELECT
            v.vendor,
            v.account_id,
            v.region,
            v.volume_id,
            'projects/' || v.account_id || '/regions/' || v.region
              || '/disks/' || v.volume_id AS resource_name
          FROM available_volumes v
          WHERE v.vendor = 'gcp'
            AND COALESCE(v.region, '') <> ''
        """
    return """
      SELECT
        v.vendor,
        v.account_id,
        v.region,
        v.volume_id,
        v.volume_id AS resource_name
      FROM available_volumes v
      UNION ALL
      SELECT
        v.vendor,
        v.account_id,
        v.region,
        v.volume_id,
        CONCAT('volume/', v.volume_id) AS resource_name
      FROM available_volumes v
      WHERE v.vendor = 'aws'
      UNION ALL
      SELECT
        v.vendor,
        v.account_id,
        v.region,
        v.volume_id,
        CONCAT(
          'arn', CHAR(58), 'aws', CHAR(58), 'ec2', CHAR(58),
          v.region, CHAR(58), v.account_id, CHAR(58), 'volume/', v.volume_id
        ) AS resource_name
      FROM available_volumes v
      WHERE v.vendor = 'aws'
      UNION ALL
      SELECT
        v.vendor,
        v.account_id,
        v.region,
        v.volume_id,
        CONCAT(
          'projects/', v.account_id, '/zones/', v.availability_zone,
          '/disks/', v.volume_id
        ) AS resource_name
      FROM available_volumes v
      WHERE v.vendor = 'gcp'
        AND COALESCE(v.availability_zone, '') <> ''
      UNION ALL
      SELECT
        v.vendor,
        v.account_id,
        v.region,
        v.volume_id,
        CONCAT(
          'projects/', v.account_id, '/regions/', v.region,
          '/disks/', v.volume_id
        ) AS resource_name
      FROM available_volumes v
      WHERE v.vendor = 'gcp'
        AND COALESCE(v.region, '') <> ''
    """


def _unattached_block_volume_item(row: Mapping[str, Any]) -> dict[str, Any]:
    first_seen = _parse_date(row["first_seen_available"])
    duration = max((_today() - first_seen).days, 0) if first_seen else None
    source_created = _parse_date(row.get("source_created_at"))
    age = max((_today() - source_created).days, 0) if source_created else duration
    matched_cost_rows = int(to_number(row.get("matched_cost_rows")) or 0)
    cost = _money(row["cost"]) if matched_cost_rows > 0 else None
    return {
        "vendor": str(row["vendor"] or ""),
        "volume_id": str(row["volume_id"]),
        "tags": _format_tags(row.get("tags_json")),
        "owner": str(row["owner"] or ""),
        "owner_status": str(row["owner_status"] or "missing"),
        "team": str(row["team"] or ""),
        "manager": str(row["manager"] or ""),
        "size_gib": _number_or_none(row["size_gib"]),
        "cost": cost,
        "duration": duration,
        "age": age,
    }


def _format_tags(value: Any) -> str:
    pairs = []
    for key, tag_value in _json_tag_pairs(value):
        pairs.append(f"{key}={tag_value}")
    return ", ".join(pairs)


def _json_tag_pairs(value: Any) -> list[tuple[str, str]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        raw_tags = value
    else:
        text_value = str(value).strip()
        if not text_value:
            return []
        try:
            raw_tags = json.loads(text_value)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw_tags, Mapping):
        return []

    pairs = []
    for raw_key in sorted(raw_tags):
        raw_value = raw_tags[raw_key]
        if raw_value is None:
            continue
        key = str(raw_key).strip()
        if not key:
            continue
        if isinstance(raw_value, (Mapping, list, tuple)):
            tag_value = json.dumps(raw_value, ensure_ascii=False, sort_keys=True)
        else:
            tag_value = str(raw_value).strip()
        if tag_value:
            pairs.append((key, tag_value))
    return pairs


def _money(value: Any) -> float:
    return round(float(to_number(value) or 0), 2)


def _number_or_none(value: Any) -> float | None:
    numeric = to_number(value)
    return float(numeric) if numeric is not None else None


def _date_text(value: Any) -> str | None:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else None


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = str(value)
    try:
        return date.fromisoformat(text_value[:10])
    except ValueError:
        return None


def _today() -> date:
    return date.today()
