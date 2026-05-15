from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass

from roster.jobs.sync_roster import FetchedRoster, RosterSource


@dataclass(frozen=True)
class RosterValidationSummary:
    groups: int
    employees: int
    root_groups: int
    groups_without_manager: int
    groups_with_missing_parent: int
    employees_without_email: int
    employees_without_employee_no: int
    employees_without_github_id: int
    employees_without_group: int
    employees_with_missing_group: int
    duplicate_employee_no: int
    duplicate_email: int
    duplicate_github_id: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def validate_lark_roster(source: RosterSource) -> RosterValidationSummary:
    roster = source.fetch_roster()
    return summarize_roster(roster)


def summarize_roster(roster: FetchedRoster) -> RosterValidationSummary:
    group_ids = {group.lark_group_id for group in roster.groups}
    employee_nos = [_normalized(employee.employee_no) for employee in roster.employees]
    emails = [_normalized(employee.email) for employee in roster.employees]
    github_ids = [_normalized(employee.github_id) for employee in roster.employees]

    return RosterValidationSummary(
        groups=len(roster.groups),
        employees=len(roster.employees),
        root_groups=sum(1 for group in roster.groups if not group.parent_lark_group_id),
        groups_without_manager=sum(1 for group in roster.groups if not group.manager_lark_id),
        groups_with_missing_parent=sum(
            1
            for group in roster.groups
            if group.parent_lark_group_id and group.parent_lark_group_id not in group_ids
        ),
        employees_without_email=sum(1 for email in emails if not email),
        employees_without_employee_no=sum(1 for employee_no in employee_nos if not employee_no),
        employees_without_github_id=sum(1 for github_id in github_ids if not github_id),
        employees_without_group=sum(1 for employee in roster.employees if not employee.group_lark_id),
        employees_with_missing_group=sum(
            1
            for employee in roster.employees
            if employee.group_lark_id and employee.group_lark_id not in group_ids
        ),
        duplicate_employee_no=_duplicate_count(employee_nos),
        duplicate_email=_duplicate_count(emails),
        duplicate_github_id=_duplicate_count(github_ids),
    )


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _duplicate_count(values: list[str | None]) -> int:
    counts = Counter(value for value in values if value)
    return sum(1 for count in counts.values() if count > 1)
