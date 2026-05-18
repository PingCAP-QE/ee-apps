from __future__ import annotations

from datetime import datetime, timedelta
from typing import get_args

import pytest
from sqlalchemy import create_engine, select

from roster.jobs.sync_roster import (
    FetchedEmployee,
    FetchedGroup,
    FetchedRoster,
    RosterSyncStatus,
    StaticRosterSource,
    SyncRosterSummary,
    employees_table,
    groups_table,
    metadata,
    run_sync_roster,
)


def test_roster_sync_status_values_are_explicit() -> None:
    assert get_args(RosterSyncStatus) == (
        "not_implemented",
        "success",
        "failed",
        "partial",
    )


def test_run_sync_roster_returns_placeholder_summary() -> None:
    summary = run_sync_roster(engine=object())

    assert summary == SyncRosterSummary(groups_seen=0, employees_seen=0, status="not_implemented")


def test_run_sync_roster_writes_groups_employees_and_paths() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    source = StaticRosterSource(
        FetchedRoster(
            groups=[
                FetchedGroup(lark_group_id="root", name="Root"),
                FetchedGroup(lark_group_id="eng", name="Engineering", parent_lark_group_id="root"),
                FetchedGroup(
                    lark_group_id="db",
                    name="Database",
                    parent_lark_group_id="eng",
                    manager_lark_id="manager",
                ),
            ],
            employees=[
                FetchedEmployee(lark_id="ceo", name="CEO", email="ceo@example.com"),
                FetchedEmployee(
                    lark_id="manager",
                    name="Manager",
                    en_name="Engineering Manager",
                    employee_no="E002",
                    email="manager@example.com",
                    github_id="manager-gh",
                    manager_lark_id="ceo",
                    group_lark_id="eng",
                ),
                FetchedEmployee(
                    lark_id="dev",
                    name="Dev",
                    employee_no="E003",
                    email="dev@example.com",
                    github_id="dev-gh",
                    join_time=_dt("2024-01-02T00:00:00"),
                    manager_lark_id="manager",
                    group_lark_id="db",
                ),
            ],
        )
    )

    summary = run_sync_roster(engine, source=source, now=_dt("2026-05-14T08:00:00"))

    assert summary == SyncRosterSummary(groups_seen=3, employees_seen=3, status="success")
    with engine.connect() as conn:
        groups = {
            row.lark_group_id: row._mapping
            for row in conn.execute(select(groups_table)).all()
        }
        employees = {
            row.lark_id: row._mapping
            for row in conn.execute(select(employees_table)).all()
        }

    assert groups["root"]["path"] == f"/{groups['root']['id']}/"
    assert groups["eng"]["parent_id"] == groups["root"]["id"]
    assert groups["eng"]["path"] == f"/{groups['root']['id']}/{groups['eng']['id']}/"
    assert groups["db"]["manager_id"] == employees["manager"]["id"]
    assert groups["db"]["path"] == f"/{groups['root']['id']}/{groups['eng']['id']}/{groups['db']['id']}/"

    assert employees["manager"]["manager_id"] == employees["ceo"]["id"]
    assert employees["manager"]["en_name"] == "Engineering Manager"
    assert employees["manager"]["manager_path"] == f"/{employees['ceo']['id']}/"
    assert employees["manager"]["group_id"] == groups["eng"]["id"]
    assert employees["manager"]["group_path"] == groups["eng"]["path"]
    assert employees["dev"]["manager_id"] == employees["manager"]["id"]
    assert employees["dev"]["manager_path"] == f"/{employees['ceo']['id']}/{employees['manager']['id']}/"
    assert employees["dev"]["group_id"] == groups["db"]["id"]
    assert employees["dev"]["group_path"] == groups["db"]["path"]
    assert employees["dev"]["join_time"] == _dt("2024-01-02T00:00:00")


def test_run_sync_roster_updates_existing_rows_without_changing_internal_ids() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)

    first = StaticRosterSource(
        FetchedRoster(
            groups=[FetchedGroup(lark_group_id="eng", name="Engineering")],
            employees=[
                FetchedEmployee(
                    lark_id="alice",
                    name="Alice",
                    en_name="Alice Example",
                    email="alice@example.com",
                    github_id="alice",
                    join_time=_dt("2024-01-02T00:00:00"),
                    group_lark_id="eng",
                )
            ],
        )
    )
    second = StaticRosterSource(
        FetchedRoster(
            groups=[FetchedGroup(lark_group_id="eng", name="Engineering Platform")],
            employees=[
                FetchedEmployee(
                    lark_id="alice",
                    name="Alice Zhang",
                    en_name="Alice Z.",
                    email="alice.zhang@example.com",
                    github_id="alicezhang",
                    join_time=_dt("2024-03-04T00:00:00"),
                    group_lark_id="eng",
                )
            ],
        )
    )

    run_sync_roster(engine, source=first, now=_dt("2026-05-14T08:00:00"))
    with engine.connect() as conn:
        original_employee_id = conn.execute(select(employees_table.c.id)).scalar_one()
        original_group_id = conn.execute(select(groups_table.c.id)).scalar_one()

    run_sync_roster(engine, source=second, now=_dt("2026-05-14T09:00:00"))

    with engine.connect() as conn:
        employee = conn.execute(select(employees_table)).one()._mapping
        group = conn.execute(select(groups_table)).one()._mapping

    assert employee["id"] == original_employee_id
    assert employee["name"] == "Alice Zhang"
    assert employee["en_name"] == "Alice Z."
    assert employee["email"] == "alice.zhang@example.com"
    assert employee["github_id"] == "alicezhang"
    assert employee["join_time"] == _dt("2024-03-04T00:00:00")
    assert group["id"] == original_group_id
    assert group["name"] == "Engineering Platform"


def test_run_sync_roster_is_idempotent_for_same_roster() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    source = StaticRosterSource(
        FetchedRoster(
            groups=[
                FetchedGroup(lark_group_id="root", name="Root"),
                FetchedGroup(lark_group_id="eng", name="Engineering", parent_lark_group_id="root"),
            ],
            employees=[
                FetchedEmployee(lark_id="ceo", name="CEO", email="ceo@example.com"),
                FetchedEmployee(
                    lark_id="alice",
                    name="Alice",
                    employee_no="E001",
                    email="alice@example.com",
                    github_id="alice-gh",
                    manager_lark_id="ceo",
                    group_lark_id="eng",
                ),
            ],
        )
    )

    run_sync_roster(engine, source=source, now=_dt("2026-05-14T08:00:00"))
    with engine.connect() as conn:
        first_groups = [
            row._mapping
            for row in conn.execute(
                select(groups_table).order_by(groups_table.c.lark_group_id)
            ).all()
        ]
        first_employees = [
            row._mapping
            for row in conn.execute(
                select(employees_table).order_by(employees_table.c.lark_id)
            ).all()
        ]

    run_sync_roster(engine, source=source, now=_dt("2026-05-14T09:00:00"))
    with engine.connect() as conn:
        second_groups = [
            row._mapping
            for row in conn.execute(
                select(groups_table).order_by(groups_table.c.lark_group_id)
            ).all()
        ]
        second_employees = [
            row._mapping
            for row in conn.execute(
                select(employees_table).order_by(employees_table.c.lark_id)
            ).all()
        ]

    assert len(second_groups) == len(first_groups) == 2
    assert len(second_employees) == len(first_employees) == 2
    assert [_stable_group(row) for row in second_groups] == [
        _stable_group(row) for row in first_groups
    ]
    assert [_stable_employee(row) for row in second_employees] == [
        _stable_employee(row) for row in first_employees
    ]


def test_run_sync_roster_marks_missing_rows_inactive_after_grace_period() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    original_time = _dt("2026-05-10T08:00:00")
    source = StaticRosterSource(
        FetchedRoster(
            groups=[FetchedGroup(lark_group_id="eng", name="Engineering")],
            employees=[FetchedEmployee(lark_id="alice", name="Alice", group_lark_id="eng")],
        )
    )

    run_sync_roster(engine, source=source, now=original_time)
    run_sync_roster(
        engine,
        source=StaticRosterSource(FetchedRoster(groups=[], employees=[])),
        now=original_time + timedelta(days=1),
        inactive_grace=timedelta(days=2),
    )
    with engine.connect() as conn:
        employee_is_active = conn.execute(select(employees_table.c.is_active)).scalar_one()
        group_is_active = conn.execute(select(groups_table.c.is_active)).scalar_one()

    assert employee_is_active is True
    assert group_is_active is True

    run_sync_roster(
        engine,
        source=StaticRosterSource(FetchedRoster(groups=[], employees=[])),
        now=original_time + timedelta(days=3),
        inactive_grace=timedelta(days=2),
    )
    with engine.connect() as conn:
        employee_is_active = conn.execute(select(employees_table.c.is_active)).scalar_one()
        group_is_active = conn.execute(select(groups_table.c.is_active)).scalar_one()

    assert employee_is_active is False
    assert group_is_active is False


def test_run_sync_roster_normalizes_empty_join_keys_to_null() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    source = StaticRosterSource(
        FetchedRoster(
            groups=[],
            employees=[
                FetchedEmployee(lark_id="alice", name="Alice", employee_no="", email="", github_id=""),
                FetchedEmployee(
                    lark_id="bob",
                    name="Bob",
                    employee_no="   ",
                    email="  ",
                    github_id="  ",
                ),
            ],
        )
    )

    run_sync_roster(engine, source=source, now=_dt("2026-05-14T08:00:00"))

    with engine.connect() as conn:
        rows = conn.execute(select(employees_table).order_by(employees_table.c.lark_id)).all()

    assert [row._mapping["employee_no"] for row in rows] == [None, None]
    assert [row._mapping["en_name"] for row in rows] == [None, None]
    assert [row._mapping["email"] for row in rows] == [None, None]
    assert [row._mapping["github_id"] for row in rows] == [None, None]


def test_run_sync_roster_nulls_duplicate_unique_join_keys() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    source = StaticRosterSource(
        FetchedRoster(
            groups=[],
            employees=[
                FetchedEmployee(
                    lark_id="alice",
                    name="Alice",
                    email="shared@example.com",
                    github_id="same-gh",
                ),
                FetchedEmployee(
                    lark_id="bob",
                    name="Bob",
                    email="shared@example.com",
                    github_id="same-gh",
                ),
                FetchedEmployee(
                    lark_id="carol",
                    name="Carol",
                    email="carol@example.com",
                    github_id="carol-gh",
                ),
            ],
        )
    )

    run_sync_roster(engine, source=source, now=_dt("2026-05-14T08:00:00"))

    with engine.connect() as conn:
        employees = {
            row.lark_id: row._mapping
            for row in conn.execute(select(employees_table).order_by(employees_table.c.lark_id))
        }

    assert set(employees) == {"alice", "bob", "carol"}
    assert employees["alice"]["email"] is None
    assert employees["bob"]["email"] is None
    assert employees["alice"]["github_id"] is None
    assert employees["bob"]["github_id"] is None
    assert employees["carol"]["email"] == "carol@example.com"
    assert employees["carol"]["github_id"] == "carol-gh"


def test_run_sync_roster_detects_group_cycle() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    source = StaticRosterSource(
        FetchedRoster(
            groups=[
                FetchedGroup(lark_group_id="a", name="A", parent_lark_group_id="b"),
                FetchedGroup(lark_group_id="b", name="B", parent_lark_group_id="a"),
            ],
            employees=[],
        )
    )

    with pytest.raises(ValueError, match="Cycle detected in roster group tree"):
        run_sync_roster(engine, source=source, now=_dt("2026-05-14T08:00:00"))


def test_run_sync_roster_detects_manager_cycle() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    source = StaticRosterSource(
        FetchedRoster(
            groups=[],
            employees=[
                FetchedEmployee(lark_id="alice", name="Alice", manager_lark_id="bob"),
                FetchedEmployee(lark_id="bob", name="Bob", manager_lark_id="alice"),
            ],
        )
    )

    with pytest.raises(ValueError, match="Cycle detected in roster manager tree"):
        run_sync_roster(engine, source=source, now=_dt("2026-05-14T08:00:00"))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _stable_group(row) -> dict[str, object]:
    return {
        "id": row["id"],
        "lark_group_id": row["lark_group_id"],
        "parent_id": row["parent_id"],
        "name": row["name"],
        "manager_id": row["manager_id"],
        "path": row["path"],
        "is_active": row["is_active"],
    }


def _stable_employee(row) -> dict[str, object]:
    return {
        "id": row["id"],
        "lark_id": row["lark_id"],
        "name": row["name"],
        "en_name": row["en_name"],
        "employee_no": row["employee_no"],
        "email": row["email"],
        "github_id": row["github_id"],
        "join_time": row["join_time"],
        "manager_id": row["manager_id"],
        "manager_path": row["manager_path"],
        "group_id": row["group_id"],
        "group_path": row["group_path"],
        "is_active": row["is_active"],
    }
