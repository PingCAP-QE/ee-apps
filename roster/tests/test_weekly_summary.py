from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine

from roster.jobs.sync_roster import employee_change_events_table, metadata
from roster.jobs.weekly_summary import (
    load_weekly_roster_summary,
    send_weekly_roster_summary_to_lark,
)
from roster.sources.lark import LarkApiClient


class FakeTransport:
    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.calls: list[dict] = []

    def request_json(self, method, path, *, params=None, json_body=None, headers=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": params or {},
                "json_body": json_body or {},
                "headers": headers or {},
            }
        )
        return self.responses.pop(0)


def test_load_weekly_roster_summary_filters_and_renders_change_types() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    now = datetime.fromisoformat("2026-05-25T01:00:00")
    with engine.begin() as connection:
        connection.execute(
            employee_change_events_table.insert(),
            [
                _event_row(
                    "old",
                    "hire",
                    employee_name="Old",
                    event_at=now - timedelta(days=8),
                ),
                _event_row(
                    "alice",
                    "hire",
                    employee_name="Alice",
                    employee_email="alice@example.com",
                    manager_name="Manager",
                    group_name="Engineering",
                    event_at=now - timedelta(days=6),
                ),
                _event_row(
                    "bob",
                    "leave",
                    employee_name="Bob",
                    employee_email="bob@example.com",
                    manager_name="Manager",
                    group_name="Database",
                    event_at=now - timedelta(days=3),
                ),
                _event_row(
                    "carol",
                    "group_change",
                    employee_name="Carol",
                    employee_email="carol@example.com",
                    manager_name="Lead",
                    previous_group_name="Engineering",
                    group_name="Database",
                    event_at=now - timedelta(days=1),
                ),
            ],
        )

    summary = load_weekly_roster_summary(engine, now=now, days=7)

    assert [item.employee_name for item in summary.hires] == ["Alice"]
    assert [item.employee_name for item in summary.leaves] == ["Bob"]
    assert [item.employee_name for item in summary.group_changes] == ["Carol"]
    rendered = summary.render_text()
    assert "入职 1 人，离职 1 人，换组 1 人" in rendered
    assert "Alice | 邮箱: alice@example.com | 主管: Manager | 部门: Engineering" in rendered
    assert "Carol | 邮箱: carol@example.com | 主管: Lead | 部门: Engineering -> Database" in rendered


def test_send_weekly_roster_summary_to_lark_sends_text_message() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    now = datetime.fromisoformat("2026-05-25T01:00:00")
    summary = load_weekly_roster_summary(engine, now=now, days=7)
    transport = FakeTransport(
        [
            {"code": 0, "tenant_access_token": "token"},
            {"code": 0, "data": {"message_id": "om_xxx"}},
        ]
    )

    send_weekly_roster_summary_to_lark(
        summary,
        client=LarkApiClient("app_id", "app_secret", transport=transport),
        open_id="ou_xxx",
    )

    message_call = transport.calls[1]
    assert message_call["method"] == "POST"
    assert message_call["path"] == "/im/v1/messages"
    assert message_call["params"] == {"receive_id_type": "open_id"}
    assert message_call["headers"] == {"Authorization": "Bearer token"}
    assert message_call["json_body"]["receive_id"] == "ou_xxx"
    assert message_call["json_body"]["msg_type"] == "text"
    content = json.loads(message_call["json_body"]["content"])
    assert "本周没有入职、离职、换组记录。" in content["text"]


def _event_row(
    employee_lark_id: str,
    event_type: str,
    *,
    employee_name: str,
    event_at: datetime,
    employee_email: str | None = None,
    manager_name: str | None = None,
    group_name: str | None = None,
    previous_group_name: str | None = None,
) -> dict[str, object]:
    return {
        "event_type": event_type,
        "employee_lark_id": employee_lark_id,
        "employee_name": employee_name,
        "employee_email": employee_email,
        "manager_name": manager_name,
        "manager_email": None,
        "group_name": group_name,
        "group_path": None,
        "previous_group_name": previous_group_name,
        "previous_group_path": None,
        "event_at": event_at,
        "created_at": event_at,
    }
