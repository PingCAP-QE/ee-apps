from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class CostSource:
    vendor: str
    account_id: str
    billing_account_id: str | None
    display_name: str | None
    is_active: bool
    source_table: str | None = None
    source_schema_version: str | None = None
    source_available_from: date | None = None


_SELECT_COST_SOURCE = text(
    """
    SELECT
      vendor,
      account_id,
      billing_account_id,
      display_name,
      is_active,
      source_table,
      source_schema_version,
      source_available_from
    FROM cost_sources
    WHERE vendor = :vendor AND account_id = :account_id
    """
)

_SELECT_ACTIVE_COST_SOURCES = text(
    """
    SELECT
      vendor,
      account_id,
      billing_account_id,
      display_name,
      is_active,
      source_table,
      source_schema_version,
      source_available_from
    FROM cost_sources
    WHERE is_active = 1
      AND (:vendor IS NULL OR vendor = :vendor)
    ORDER BY vendor, account_id
    """
)


def get_cost_source(
    connection: Connection,
    *,
    vendor: str,
    account_id: str,
) -> CostSource | None:
    row = (
        connection.execute(
            _SELECT_COST_SOURCE,
            {"vendor": vendor, "account_id": account_id},
        )
        .mappings()
        .first()
    )
    return _coerce_cost_source(row) if row is not None else None


def list_active_cost_sources(
    connection: Connection,
    *,
    vendor: str | None = None,
) -> tuple[CostSource, ...]:
    rows = connection.execute(_SELECT_ACTIVE_COST_SOURCES, {"vendor": vendor}).mappings()
    return tuple(_coerce_cost_source(row) for row in rows)


def list_active_direct_summary_cost_sources(
    connection: Connection,
    *,
    vendor: str | None = None,
) -> tuple[CostSource, ...]:
    """Sources eligible for the generic summary -> attribution writer.

    Tencent's native published projection deliberately is not a Stage-0 fact.
    Keep this filter at discovery time in addition to the defensive library gate.
    """
    return tuple(
        source
        for source in list_active_cost_sources(connection, vendor=vendor)
        if attribution_write_mode(connection, vendor=source.vendor, account_id=source.account_id)
        == "direct_summary"
    )


def attribution_write_mode(
    connection: Connection,
    *,
    vendor: str,
    account_id: str,
) -> str:
    if not _has_column(connection, "cost_sources", "attribution_write_mode"):
        # Compatibility for databases before migration 026. A migrated Tencent
        # source is always terminal; callers must not silently assume that policy.
        return "direct_summary"
    value = connection.execute(
        text(
            """
            SELECT attribution_write_mode FROM cost_sources
            WHERE vendor=:vendor AND account_id=:account_id
            """
        ),
        {"vendor": vendor, "account_id": account_id},
    ).scalar_one_or_none()
    return str(value or "direct_summary")


def ensure_direct_summary_source(
    engine,
    *,
    vendor: str,
    account_id: str,
) -> None:
    """Reject terminal sources before job state, deletes, or publications change."""
    if not hasattr(engine, "begin"):
        return
    with engine.begin() as connection:
        mode = attribution_write_mode(connection, vendor=vendor, account_id=account_id)
    if mode != "direct_summary":
        raise ValueError(
            f"Cost source {vendor}/{account_id} uses {mode}; "
            "use materialize-tencent-ci-cost-allocation instead"
        )


def ensure_tencent_terminal_source_policy(
    connection: Connection,
    *,
    account_id: str,
) -> None:
    """Persist the Tencent native projection writer gate when the source is first created."""
    if not _has_column(connection, "cost_sources", "attribution_write_mode"):
        return
    connection.execute(
        text(
            """
            UPDATE cost_sources SET attribution_write_mode='tencent_ci_published_terminal'
            WHERE vendor='tencent' AND account_id=:account_id
              AND attribution_write_mode='direct_summary'
            """
        ),
        {"account_id": account_id},
    )


def terminal_cost_source_keys(connection: Connection) -> frozenset[tuple[str, str]]:
    if not _has_column(connection, "cost_sources", "attribution_write_mode"):
        return frozenset()
    rows = connection.execute(
        text(
            """
            SELECT vendor, account_id FROM cost_sources
            WHERE attribution_write_mode <> 'direct_summary'
            """
        )
    ).all()
    return frozenset((str(row[0]), str(row[1])) for row in rows)


def _has_column(connection: Connection, table: str, column: str) -> bool:
    if connection.dialect.name == "sqlite":
        return any(
            str(row[1]) == column
            for row in connection.execute(text(f"PRAGMA table_info({table})"))
        )
    return connection.execute(
        text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema=DATABASE() AND table_name=:table AND column_name=:column
            LIMIT 1
            """
        ),
        {"table": table, "column": column},
    ).first() is not None


def ensure_cost_source_enabled(
    connection: Connection,
    *,
    vendor: str,
    account_id: str,
    dry_run: bool,
    display_name: str | None = None,
) -> None:
    source = get_cost_source(connection, vendor=vendor, account_id=account_id)
    if source is not None:
        if not source.is_active and not dry_run:
            raise ValueError(f"Cost source {vendor}/{account_id} is inactive")
        return
    if not dry_run:
        upsert_cost_source(
            connection,
            vendor=vendor,
            account_id=account_id,
            display_name=display_name or account_id,
        )


def upsert_cost_source(
    connection: Connection,
    *,
    vendor: str,
    account_id: str,
    billing_account_id: str | None = None,
    display_name: str | None = None,
) -> None:
    connection.execute(
        _build_upsert_cost_source_statement(connection),
        {
            "vendor": vendor,
            "account_id": account_id,
            "billing_account_id": billing_account_id,
            "display_name": display_name or account_id,
        },
    )


def _coerce_cost_source(row: Any) -> CostSource:
    return CostSource(
        vendor=str(row["vendor"]),
        account_id=str(row["account_id"]),
        billing_account_id=row["billing_account_id"],
        display_name=row["display_name"],
        is_active=bool(int(row["is_active"])),
        source_table=row.get("source_table"),
        source_schema_version=row.get("source_schema_version"),
        source_available_from=_coerce_date(row.get("source_available_from")),
    )


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _build_upsert_cost_source_statement(connection: Connection):
    if connection.dialect.name == "sqlite":
        return text(
            """
            INSERT INTO cost_sources (
              vendor,
              account_id,
              billing_account_id,
              display_name,
              is_active,
              updated_at
            ) VALUES (
              :vendor,
              :account_id,
              :billing_account_id,
              :display_name,
              1,
              CURRENT_TIMESTAMP
            )
            ON CONFLICT(vendor, account_id) DO UPDATE SET
              billing_account_id = COALESCE(
                excluded.billing_account_id,
                cost_sources.billing_account_id
              ),
              display_name = COALESCE(cost_sources.display_name, excluded.display_name),
              updated_at = CURRENT_TIMESTAMP
            """
        )
    return text(
        """
        INSERT INTO cost_sources (
          vendor,
          account_id,
          billing_account_id,
          display_name,
          is_active
        ) VALUES (
          :vendor,
          :account_id,
          :billing_account_id,
          :display_name,
          1
        )
        ON DUPLICATE KEY UPDATE
          billing_account_id = COALESCE(VALUES(billing_account_id), billing_account_id),
          display_name = COALESCE(display_name, VALUES(display_name)),
          updated_at = CURRENT_TIMESTAMP
        """
    )
