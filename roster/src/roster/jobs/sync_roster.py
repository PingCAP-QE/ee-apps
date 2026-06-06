from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    bindparam,
    func,
    select,
    update,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

LOG = logging.getLogger(__name__)

RosterSyncStatus = Literal["not_implemented", "success", "failed", "partial"]
RosterEmployeeChangeType = Literal["hire", "leave", "group_change"]
DEFAULT_INACTIVE_GRACE = timedelta(days=2)
BULK_UPDATE_CHUNK_SIZE = 500

metadata = MetaData()


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

employees_table = Table(
    "roster_employees",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("lark_id", String(128), nullable=False, unique=True),
    Column("name", String(255), nullable=False),
    Column("en_name", String(255)),
    Column("employee_no", String(64)),
    Column("email", String(255), unique=True),
    Column("github_id", String(255), unique=True),
    Column("join_time", DateTime),
    Column("manager_id", BigInteger),
    Column("manager_path", String(1024)),
    Column("group_id", BigInteger),
    Column("group_path", String(1024)),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("last_seen_at", DateTime),
    Column("created_at", DateTime, nullable=False, default=_utcnow_naive),
    Column("updated_at", DateTime, nullable=False, default=_utcnow_naive),
)

groups_table = Table(
    "roster_groups",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("lark_group_id", String(128), nullable=False, unique=True),
    Column("parent_id", BigInteger),
    Column("name", String(255), nullable=False),
    Column("manager_id", BigInteger),
    Column("path", String(1024)),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("last_seen_at", DateTime),
    Column("created_at", DateTime, nullable=False, default=_utcnow_naive),
    Column("updated_at", DateTime, nullable=False, default=_utcnow_naive),
)

employee_change_events_table = Table(
    "roster_employee_change_events",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("event_type", String(32), nullable=False),
    Column("employee_lark_id", String(128), nullable=False),
    Column("employee_name", String(255), nullable=False),
    Column("employee_email", String(255)),
    Column("manager_name", String(255)),
    Column("manager_email", String(255)),
    Column("group_name", String(255)),
    Column("group_path", String(1024)),
    Column("previous_group_name", String(255)),
    Column("previous_group_path", String(1024)),
    Column("event_at", DateTime, nullable=False),
    Column("created_at", DateTime, nullable=False, default=_utcnow_naive),
)


@dataclass(frozen=True)
class SyncRosterSummary:
    """Summary for a roster sync run."""

    groups_seen: int
    employees_seen: int
    status: RosterSyncStatus


@dataclass(frozen=True)
class FetchedEmployee:
    lark_id: str
    name: str
    en_name: str | None = None
    employee_no: str | None = None
    email: str | None = None
    github_id: str | None = None
    join_time: datetime | None = None
    manager_lark_id: str | None = None
    group_lark_id: str | None = None


@dataclass(frozen=True)
class FetchedGroup:
    lark_group_id: str
    name: str
    parent_lark_group_id: str | None = None
    manager_lark_id: str | None = None


@dataclass(frozen=True)
class FetchedRoster:
    groups: Sequence[FetchedGroup] = field(default_factory=tuple)
    employees: Sequence[FetchedEmployee] = field(default_factory=tuple)


class RosterSource(Protocol):
    def fetch_roster(self) -> FetchedRoster: ...


@dataclass(frozen=True)
class StaticRosterSource:
    roster: FetchedRoster

    def fetch_roster(self) -> FetchedRoster:
        return self.roster


def run_sync_roster(
    engine: Any,
    *,
    source: RosterSource | None = None,
    now: datetime | None = None,
    inactive_grace: timedelta = DEFAULT_INACTIVE_GRACE,
) -> SyncRosterSummary:
    if source is None:
        summary = SyncRosterSummary(groups_seen=0, employees_seen=0, status="not_implemented")
        LOG.warning("sync-roster is not implemented yet", extra={"summary": summary.__dict__})
        return summary

    sync_time = now or _utcnow_naive()
    roster = source.fetch_roster()
    with engine.begin() as connection:
        _sync_roster_rows(
            connection,
            roster=roster,
            sync_time=sync_time,
            inactive_grace=inactive_grace,
        )

    return SyncRosterSummary(
        groups_seen=len(roster.groups),
        employees_seen=len(roster.employees),
        status="success",
    )


def _sync_roster_rows(
    connection: Connection,
    *,
    roster: FetchedRoster,
    sync_time: datetime,
    inactive_grace: timedelta,
) -> None:
    previous_employee_state_by_lark_id = _employee_state_map(connection)
    group_rows = [_group_pass1_row(group, sync_time) for group in roster.groups]
    employee_rows = _employee_pass1_rows(roster.employees, sync_time)

    _upsert_rows(connection, groups_table, group_rows, key_columns=("lark_group_id",))
    _upsert_rows(connection, employees_table, employee_rows, key_columns=("lark_id",))

    employee_id_by_lark_id = _employee_id_map(connection)
    group_id_by_lark_group_id = _group_id_map(connection)

    group_path_by_id = _update_group_references(
        connection,
        groups=roster.groups,
        employee_id_by_lark_id=employee_id_by_lark_id,
        group_id_by_lark_group_id=group_id_by_lark_group_id,
    )
    _update_employee_references(
        connection,
        employees=roster.employees,
        employee_id_by_lark_id=employee_id_by_lark_id,
        group_id_by_lark_group_id=group_id_by_lark_group_id,
        group_path_by_id=group_path_by_id,
    )
    _mark_stale_rows_inactive(
        connection,
        sync_time=sync_time,
        inactive_grace=inactive_grace,
    )
    _record_employee_change_events(
        connection,
        previous_employee_state_by_lark_id=previous_employee_state_by_lark_id,
        current_employee_state_by_lark_id=_employee_state_map(connection),
        fetched_employee_lark_ids={employee.lark_id for employee in roster.employees},
        event_at=sync_time,
    )


def _group_pass1_row(group: FetchedGroup, sync_time: datetime) -> dict[str, object]:
    return {
        "lark_group_id": group.lark_group_id,
        "name": group.name,
        "parent_id": None,
        "manager_id": None,
        "path": None,
        "is_active": True,
        "last_seen_at": sync_time,
        "updated_at": sync_time,
    }


def _employee_pass1_rows(
    employees: Sequence[FetchedEmployee],
    sync_time: datetime,
) -> list[dict[str, object]]:
    email_counts = Counter(
        normalized
        for employee in employees
        if (normalized := _nullable_text(employee.email)) is not None
    )
    github_id_counts = Counter(
        normalized
        for employee in employees
        if (normalized := _nullable_text(employee.github_id)) is not None
    )
    duplicate_emails = {value for value, count in email_counts.items() if count > 1}
    duplicate_github_ids = {value for value, count in github_id_counts.items() if count > 1}
    if duplicate_emails or duplicate_github_ids:
        LOG.warning(
            "dropping duplicate roster join keys",
            extra={
                "duplicate_email_count": len(duplicate_emails),
                "duplicate_github_id_count": len(duplicate_github_ids),
            },
        )

    rows = []
    for employee in employees:
        email = _nullable_text(employee.email)
        github_id = _nullable_text(employee.github_id)
        rows.append(
            {
                "lark_id": employee.lark_id,
                "name": employee.name,
                "en_name": _nullable_text(employee.en_name),
                "employee_no": _nullable_text(employee.employee_no),
                "email": None if email in duplicate_emails else email,
                "github_id": None if github_id in duplicate_github_ids else github_id,
                "join_time": employee.join_time,
                "manager_id": None,
                "manager_path": None,
                "group_id": None,
                "group_path": None,
                "is_active": True,
                "last_seen_at": sync_time,
                "updated_at": sync_time,
            }
        )
    return rows


def _nullable_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _upsert_rows(
    connection: Connection,
    table: Table,
    rows: Sequence[dict[str, object]],
    *,
    key_columns: Sequence[str],
) -> None:
    if not rows:
        return

    dialect = connection.dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(list(rows))
        excluded = statement.excluded
        update_values = {
            column.name: getattr(excluded, column.name)
            for column in table.columns
            if column.name not in {"id", "created_at", *key_columns}
        }
        _preserve_existing_github_id_when_source_is_empty(table, update_values, excluded)
        connection.execute(statement.on_conflict_do_update(index_elements=key_columns, set_=update_values))
        return

    if dialect == "mysql":
        statement = mysql_insert(table).values(list(rows))
        inserted = statement.inserted
        update_values = {
            column.name: getattr(inserted, column.name)
            for column in table.columns
            if column.name not in {"id", "created_at", *key_columns}
        }
        _preserve_existing_github_id_when_source_is_empty(table, update_values, inserted)
        connection.execute(statement.on_duplicate_key_update(**update_values))
        return

    raise RuntimeError(f"Unsupported database dialect for roster sync: {dialect}")


def _preserve_existing_github_id_when_source_is_empty(
    table: Table,
    update_values: dict[str, object],
    incoming: Any,
) -> None:
    if table.name != employees_table.name or "github_id" not in update_values:
        return

    update_values["github_id"] = func.coalesce(incoming.github_id, table.c.github_id)


def _employee_id_map(connection: Connection) -> dict[str, int]:
    rows = connection.execute(select(employees_table.c.lark_id, employees_table.c.id)).all()
    return {str(row.lark_id): int(row.id) for row in rows}


def _group_id_map(connection: Connection) -> dict[str, int]:
    rows = connection.execute(select(groups_table.c.lark_group_id, groups_table.c.id)).all()
    return {str(row.lark_group_id): int(row.id) for row in rows}


def _update_group_references(
    connection: Connection,
    *,
    groups: Sequence[FetchedGroup],
    employee_id_by_lark_id: dict[str, int],
    group_id_by_lark_group_id: dict[str, int],
) -> dict[int, str]:
    group_by_lark_group_id = {group.lark_group_id: group for group in groups}
    path_by_lark_group_id: dict[str, str] = {}
    update_rows: list[dict[str, object]] = []

    def build_path(lark_group_id: str, visiting: set[str] | None = None) -> str:
        if lark_group_id in path_by_lark_group_id:
            return path_by_lark_group_id[lark_group_id]
        visiting = visiting or set()
        if lark_group_id in visiting:
            raise ValueError(f"Cycle detected in roster group tree at {lark_group_id!r}")
        visiting.add(lark_group_id)
        group = group_by_lark_group_id[lark_group_id]
        group_id = group_id_by_lark_group_id[lark_group_id]
        if group.parent_lark_group_id and group.parent_lark_group_id in group_id_by_lark_group_id:
            path = f"{build_path(group.parent_lark_group_id, visiting)}{group_id}/"
        else:
            path = f"/{group_id}/"
        path_by_lark_group_id[lark_group_id] = path
        visiting.remove(lark_group_id)
        return path

    for group in groups:
        group_id = group_id_by_lark_group_id[group.lark_group_id]
        parent_id = (
            group_id_by_lark_group_id.get(group.parent_lark_group_id)
            if group.parent_lark_group_id
            else None
        )
        manager_id = (
            employee_id_by_lark_id.get(group.manager_lark_id)
            if group.manager_lark_id
            else None
        )
        path = build_path(group.lark_group_id)
        update_rows.append(
            {
                "id": group_id,
                "parent_id": parent_id,
                "manager_id": manager_id,
                "path": path,
            }
        )

    _bulk_update_by_id(
        connection,
        groups_table,
        update_rows,
        value_columns=("parent_id", "manager_id", "path"),
    )
    return {group_id_by_lark_group_id[lark_id]: path for lark_id, path in path_by_lark_group_id.items()}


def _update_employee_references(
    connection: Connection,
    *,
    employees: Sequence[FetchedEmployee],
    employee_id_by_lark_id: dict[str, int],
    group_id_by_lark_group_id: dict[str, int],
    group_path_by_id: dict[int, str],
) -> None:
    employee_by_lark_id = {employee.lark_id: employee for employee in employees}
    manager_path_by_lark_id: dict[str, str] = {}
    update_rows: list[dict[str, object]] = []

    def build_manager_path(lark_id: str, visiting: set[str] | None = None) -> str:
        if lark_id in manager_path_by_lark_id:
            return manager_path_by_lark_id[lark_id]
        visiting = visiting or set()
        if lark_id in visiting:
            raise ValueError(f"Cycle detected in roster manager tree at {lark_id!r}")
        visiting.add(lark_id)
        employee = employee_by_lark_id[lark_id]
        if employee.manager_lark_id and employee.manager_lark_id in employee_id_by_lark_id:
            manager_path = build_manager_path(employee.manager_lark_id, visiting)
            manager_id = employee_id_by_lark_id[employee.manager_lark_id]
            path = f"{manager_path}{manager_id}/" if manager_path else f"/{manager_id}/"
        else:
            path = None
        manager_path_by_lark_id[lark_id] = path or ""
        visiting.remove(lark_id)
        return manager_path_by_lark_id[lark_id]

    for employee in employees:
        employee_id = employee_id_by_lark_id[employee.lark_id]
        manager_id = (
            employee_id_by_lark_id.get(employee.manager_lark_id)
            if employee.manager_lark_id
            else None
        )
        group_id = (
            group_id_by_lark_group_id.get(employee.group_lark_id)
            if employee.group_lark_id
            else None
        )
        manager_path = build_manager_path(employee.lark_id) or None
        group_path = group_path_by_id.get(group_id) if group_id else None
        update_rows.append(
            {
                "id": employee_id,
                "manager_id": manager_id,
                "manager_path": manager_path,
                "group_id": group_id,
                "group_path": group_path,
            }
        )

    _bulk_update_by_id(
        connection,
        employees_table,
        update_rows,
        value_columns=("manager_id", "manager_path", "group_id", "group_path"),
    )


def _bulk_update_by_id(
    connection: Connection,
    table: Table,
    rows: Sequence[dict[str, object]],
    *,
    value_columns: Sequence[str],
) -> None:
    if not rows:
        return

    statement = (
        update(table)
        .where(table.c.id == bindparam("_target_id"))
        .values({column: bindparam(f"_{column}") for column in value_columns})
    )
    for chunk in _chunks(rows, BULK_UPDATE_CHUNK_SIZE):
        connection.execute(
            statement,
            [
                {
                    "_target_id": row["id"],
                    **{f"_{column}": row[column] for column in value_columns},
                }
                for row in chunk
            ],
        )


def _record_employee_change_events(
    connection: Connection,
    *,
    previous_employee_state_by_lark_id: dict[str, dict[str, object]],
    current_employee_state_by_lark_id: dict[str, dict[str, object]],
    fetched_employee_lark_ids: set[str],
    event_at: datetime,
) -> None:
    event_rows: list[dict[str, object]] = []

    for lark_id in sorted(fetched_employee_lark_ids):
        current = current_employee_state_by_lark_id.get(lark_id)
        if current is None:
            continue
        previous = previous_employee_state_by_lark_id.get(lark_id)
        if previous is None or not _state_is_active(previous):
            event_rows.append(_employee_change_event_row("hire", current, event_at=event_at))
        elif previous.get("group_id") != current.get("group_id"):
            event_rows.append(
                _employee_change_event_row(
                    "group_change",
                    current,
                    event_at=event_at,
                    previous=previous,
                )
            )

    for lark_id, previous in sorted(previous_employee_state_by_lark_id.items()):
        if lark_id in fetched_employee_lark_ids or not _state_is_active(previous):
            continue
        current = current_employee_state_by_lark_id.get(lark_id)
        if current is not None and not _state_is_active(current):
            event_rows.append(_employee_change_event_row("leave", current, event_at=event_at))

    if event_rows:
        connection.execute(employee_change_events_table.insert(), event_rows)


def _employee_change_event_row(
    event_type: RosterEmployeeChangeType,
    current: dict[str, object],
    *,
    event_at: datetime,
    previous: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "event_type": event_type,
        "employee_lark_id": current["lark_id"],
        "employee_name": current["name"],
        "employee_email": current.get("email"),
        "manager_name": current.get("manager_name"),
        "manager_email": current.get("manager_email"),
        "group_name": current.get("group_name"),
        "group_path": current.get("group_path"),
        "previous_group_name": previous.get("group_name") if previous else None,
        "previous_group_path": previous.get("group_path") if previous else None,
        "event_at": event_at,
        "created_at": event_at,
    }


def _employee_state_map(connection: Connection) -> dict[str, dict[str, object]]:
    manager = employees_table.alias("manager")
    rows = connection.execute(
        select(
            employees_table.c.lark_id,
            employees_table.c.name,
            employees_table.c.email,
            employees_table.c.is_active,
            employees_table.c.manager_id,
            employees_table.c.group_id,
            employees_table.c.group_path,
            manager.c.name.label("manager_name"),
            manager.c.email.label("manager_email"),
            groups_table.c.name.label("group_name"),
        )
        .select_from(
            employees_table.outerjoin(manager, employees_table.c.manager_id == manager.c.id)
            .outerjoin(groups_table, employees_table.c.group_id == groups_table.c.id)
        )
    ).all()
    return {
        str(row.lark_id): dict(row._mapping)
        for row in rows
    }


def _state_is_active(state: dict[str, object]) -> bool:
    return bool(state.get("is_active"))


def _chunks(
    rows: Sequence[dict[str, object]],
    size: int,
) -> list[Sequence[dict[str, object]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def _mark_stale_rows_inactive(
    connection: Connection,
    *,
    sync_time: datetime,
    inactive_grace: timedelta,
) -> None:
    cutoff = sync_time - inactive_grace
    stale_filter = and_(
        employees_table.c.is_active.is_(True),
        employees_table.c.last_seen_at.is_not(None),
        employees_table.c.last_seen_at < cutoff,
    )
    connection.execute(update(employees_table).where(stale_filter).values(is_active=False))
    connection.execute(
        update(groups_table)
        .where(
            and_(
                groups_table.c.is_active.is_(True),
                groups_table.c.last_seen_at.is_not(None),
                groups_table.c.last_seen_at < cutoff,
            )
        )
        .values(is_active=False)
    )
