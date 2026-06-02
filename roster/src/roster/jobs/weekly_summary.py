from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from roster.jobs.sync_roster import employee_change_events_table
from roster.sources.lark import LarkApiClient

DEFAULT_WEEKLY_SUMMARY_DAYS = 7
DEFAULT_DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class LarkRecipient:
    receive_id: str


@dataclass(frozen=True)
class RosterChangeItem:
    event_type: str
    employee_name: str
    employee_email: str | None
    manager_name: str | None
    manager_email: str | None
    group_name: str | None
    previous_group_name: str | None
    event_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "employee_name": self.employee_name,
            "employee_email": self.employee_email,
            "manager_name": self.manager_name,
            "manager_email": self.manager_email,
            "group_name": self.group_name,
            "previous_group_name": self.previous_group_name,
            "event_at": self.event_at.isoformat(),
        }


@dataclass(frozen=True)
class WeeklyRosterSummary:
    since: datetime
    until: datetime
    hires: Sequence[RosterChangeItem]
    leaves: Sequence[RosterChangeItem]
    group_changes: Sequence[RosterChangeItem]

    @property
    def total_count(self) -> int:
        return len(self.hires) + len(self.leaves) + len(self.group_changes)

    def to_dict(self) -> dict[str, object]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "hires": [item.to_dict() for item in self.hires],
            "leaves": [item.to_dict() for item in self.leaves],
            "group_changes": [item.to_dict() for item in self.group_changes],
        }

    def render_text(self) -> str:
        since = _format_display_date(self.since)
        until = _format_display_date(self.until)
        lines = [
            f"Roster 周报（{since} ~ {until}）",
            f"入职 {len(self.hires)} 人，离职 {len(self.leaves)} 人，换组 {len(self.group_changes)} 人",
        ]
        if self.total_count == 0:
            lines.append("本周没有入职、离职、换组记录。")
            return "\n".join(lines)

        _append_section(lines, "入职", self.hires)
        _append_section(lines, "离职", self.leaves)
        _append_section(lines, "换组", self.group_changes)
        return "\n".join(lines)


def load_weekly_roster_summary(
    engine: Engine,
    *,
    now: datetime | None = None,
    days: int = DEFAULT_WEEKLY_SUMMARY_DAYS,
) -> WeeklyRosterSummary:
    until = now or datetime.now(UTC).replace(tzinfo=None)
    since = until - timedelta(days=days)
    with engine.connect() as connection:
        rows = connection.execute(
            select(employee_change_events_table)
            .where(
                and_(
                    employee_change_events_table.c.event_at >= since,
                    employee_change_events_table.c.event_at < until,
                )
            )
            .order_by(
                employee_change_events_table.c.event_type,
                employee_change_events_table.c.event_at,
                employee_change_events_table.c.employee_name,
            )
        ).all()

    items = [_row_to_change_item(row._mapping) for row in rows]
    return WeeklyRosterSummary(
        since=since,
        until=until,
        hires=[item for item in items if item.event_type == "hire"],
        leaves=[item for item in items if item.event_type == "leave"],
        group_changes=[item for item in items if item.event_type == "group_change"],
    )


def send_weekly_roster_summary_to_lark(
    summary: WeeklyRosterSummary,
    *,
    client: LarkApiClient,
    open_id: str,
) -> None:
    send_weekly_roster_summary_to_lark_recipients(
        summary,
        client=client,
        open_ids=[open_id],
    )


def send_weekly_roster_summary_to_lark_recipients(
    summary: WeeklyRosterSummary,
    *,
    client: LarkApiClient,
    open_ids: Sequence[str] = (),
) -> None:
    recipients = _build_lark_recipients(open_ids=open_ids)
    if not recipients:
        raise ValueError("at least one Lark recipient is required")

    token = client.tenant_access_token()
    content = json.dumps({"text": summary.render_text()}, ensure_ascii=False)
    for recipient in recipients:
        client.post(
            "/im/v1/messages",
            params={"receive_id_type": "open_id"},
            json_body={
                "receive_id": recipient.receive_id,
                "msg_type": "text",
                "content": content,
            },
            token=token,
        )


def _row_to_change_item(row) -> RosterChangeItem:
    return RosterChangeItem(
        event_type=row["event_type"],
        employee_name=row["employee_name"],
        employee_email=row["employee_email"],
        manager_name=row["manager_name"],
        manager_email=row["manager_email"],
        group_name=row["group_name"],
        previous_group_name=row["previous_group_name"],
        event_at=row["event_at"],
    )


def _append_section(
    lines: list[str],
    title: str,
    items: Sequence[RosterChangeItem],
) -> None:
    if not items:
        return
    lines.append("")
    lines.append(f"{title}:")
    for item in items:
        lines.append(f"- {_format_change_item(item)}")


def _format_change_item(item: RosterChangeItem) -> str:
    manager = _person_text(item.manager_name, item.manager_email)
    department = item.group_name or "-"
    if item.event_type == "group_change":
        previous_department = item.previous_group_name or "-"
        department = f"{previous_department} -> {department}"
    return (
        f"{item.employee_name} | 邮箱: {item.employee_email or '-'} | "
        f"主管: {manager} | 部门: {department}"
    )


def _person_text(name: str | None, email: str | None) -> str:
    if name and email:
        return f"{name} <{email}>"
    return name or email or "-"


def _format_display_date(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC)
    return aware.astimezone(DEFAULT_DISPLAY_TIMEZONE).strftime("%Y-%m-%d")


def _build_lark_recipients(
    *,
    open_ids: Sequence[str],
) -> list[LarkRecipient]:
    recipients: list[LarkRecipient] = []
    seen: set[str] = set()

    for value in open_ids:
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        recipients.append(LarkRecipient(receive_id=stripped))

    return recipients
