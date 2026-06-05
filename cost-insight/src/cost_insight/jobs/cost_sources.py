from __future__ import annotations

from dataclasses import dataclass
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


_SELECT_COST_SOURCE = text(
    """
    SELECT vendor, account_id, billing_account_id, display_name, is_active
    FROM cost_sources
    WHERE vendor = :vendor AND account_id = :account_id
    """
)

_SELECT_ACTIVE_COST_SOURCES = text(
    """
    SELECT vendor, account_id, billing_account_id, display_name, is_active
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
        if not source.is_active:
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
    )


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
