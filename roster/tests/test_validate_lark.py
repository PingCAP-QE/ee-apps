from __future__ import annotations

from roster.jobs.sync_roster import FetchedEmployee, FetchedGroup, FetchedRoster, StaticRosterSource
from roster.jobs.validate_lark import RosterValidationSummary, validate_lark_roster


def test_validate_lark_roster_summarizes_field_quality() -> None:
    source = StaticRosterSource(
        FetchedRoster(
            groups=[
                FetchedGroup(lark_group_id="root", name="Root"),
                FetchedGroup(lark_group_id="eng", name="Engineering", parent_lark_group_id="root"),
                FetchedGroup(lark_group_id="sales", name="Sales", parent_lark_group_id="missing"),
                FetchedGroup(lark_group_id="ops", name="Ops"),
            ],
            employees=[
                FetchedEmployee(
                    lark_id="alice",
                    name="Alice",
                    employee_no="E001",
                    email="alice@example.com",
                    github_id="alice-gh",
                    group_lark_id="eng",
                ),
                FetchedEmployee(
                    lark_id="bob",
                    name="Bob",
                    employee_no="E001",
                    email="bob@example.com",
                    group_lark_id="missing",
                ),
                FetchedEmployee(
                    lark_id="carol",
                    name="Carol",
                    github_id="alice-gh",
                ),
            ],
        )
    )

    summary = validate_lark_roster(source)

    assert summary == RosterValidationSummary(
        groups=4,
        employees=3,
        root_groups=2,
        groups_without_manager=4,
        groups_with_missing_parent=1,
        employees_without_email=1,
        employees_without_employee_no=1,
        employees_without_github_id=1,
        employees_without_group=1,
        employees_with_missing_group=1,
        duplicate_employee_no=1,
        duplicate_email=0,
        duplicate_github_id=1,
    )
    assert summary.to_dict()["employees"] == 3
