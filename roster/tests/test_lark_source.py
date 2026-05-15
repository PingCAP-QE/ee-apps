from __future__ import annotations

import urllib.error

import pytest

from roster.sources import lark
from roster.sources.lark import LarkApiClient, LarkRosterSource, UrllibLarkTransport


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


def test_lark_roster_source_fetches_token_departments_and_users() -> None:
    transport = FakeTransport(
        [
            {"code": 0, "tenant_access_token": "token"},
            {
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [
                        {
                            "open_department_id": "od-root",
                            "parent_department_id": "0",
                            "name": "Root",
                            "leader_user_id": "ceo",
                        },
                        {
                            "open_department_id": "od-eng",
                            "parent_department_id": "od-root",
                            "name": "Engineering",
                            "leader_user_id": "manager",
                        },
                    ],
                },
            },
            {
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [
                        {
                            "union_id": "ceo",
                            "name": "CEO",
                            "email": "ceo@example.com",
                            "employee_no": "E001",
                            "leader_user_id": "",
                            "department_ids": ["od-root"],
                            "orders": [
                                {
                                    "department_id": "od-root",
                                    "department_order": 10,
                                    "is_primary_dept": True,
                                }
                            ],
                        }
                    ],
                },
            },
            {
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [
                        {
                            "union_id": "manager",
                            "name": "Manager",
                            "enterprise_email": "manager@example.com",
                            "employee_no": "E002",
                            "leader_user_id": "ceo",
                            "department_ids": ["od-eng", "od-root"],
                            "orders": [
                                {
                                    "department_id": "od-root",
                                    "department_order": 1,
                                    "is_primary_dept": False,
                                },
                                {
                                    "department_id": "od-eng",
                                    "department_order": 20,
                                    "is_primary_dept": True,
                                },
                            ],
                            "custom_attrs": [
                                {
                                    "id": "github_attr",
                                    "type": "TEXT",
                                    "value": {"text": "manager-gh"},
                                }
                            ],
                        }
                    ],
                },
            },
        ]
    )
    source = LarkRosterSource(
        LarkApiClient("app_id", "app_secret", transport=transport),
        github_custom_attr_id="github_attr",
    )

    roster = source.fetch_roster()

    assert [group.lark_group_id for group in roster.groups] == ["od-root", "od-eng"]
    assert roster.groups[0].parent_lark_group_id is None
    assert roster.groups[0].manager_lark_id == "ceo"
    assert roster.groups[1].parent_lark_group_id == "od-root"
    assert roster.groups[1].manager_lark_id == "manager"
    assert [employee.lark_id for employee in roster.employees] == ["ceo", "manager"]
    assert roster.employees[0].group_lark_id == "od-root"
    assert roster.employees[1].email == "manager@example.com"
    assert roster.employees[1].github_id == "manager-gh"
    assert roster.employees[1].manager_lark_id == "ceo"
    assert roster.employees[1].group_lark_id == "od-eng"
    assert transport.calls[0] == {
        "method": "POST",
        "path": "/auth/v3/tenant_access_token/internal",
        "params": {},
        "json_body": {"app_id": "app_id", "app_secret": "app_secret"},
        "headers": {},
    }
    assert transport.calls[1]["path"] == "/contact/v3/departments/0/children"
    assert transport.calls[1]["params"]["fetch_child"] is True
    assert transport.calls[1]["params"]["department_id_type"] == "open_department_id"
    assert transport.calls[1]["params"]["user_id_type"] == "union_id"
    assert transport.calls[2]["path"] == "/contact/v3/users/find_by_department"
    assert transport.calls[2]["params"]["department_id"] == "od-root"
    assert transport.calls[3]["params"]["department_id"] == "od-eng"
    assert all(call["headers"].get("Authorization") == "Bearer token" for call in transport.calls[1:])


def test_lark_roster_source_handles_pagination_and_deduplicates_users() -> None:
    transport = FakeTransport(
        [
            {"code": 0, "tenant_access_token": "token"},
            {
                "code": 0,
                "data": {
                    "has_more": True,
                    "page_token": "next-dept",
                    "items": [{"open_department_id": "od-a", "name": "A"}],
                },
            },
            {
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [{"open_department_id": "od-b", "name": "B"}],
                },
            },
            {
                "code": 0,
                "data": {
                    "has_more": True,
                    "page_token": "next-user",
                    "items": [{"union_id": "alice", "name": "Alice", "department_ids": ["od-a"]}],
                },
            },
            {
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [{"union_id": "alice", "name": "Alice v2", "department_ids": ["od-a"]}],
                },
            },
            {
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [{"union_id": "bob", "name": "Bob", "department_ids": ["od-b"]}],
                },
            },
        ]
    )
    source = LarkRosterSource(LarkApiClient("app_id", "app_secret", transport=transport))

    roster = source.fetch_roster()

    assert [group.lark_group_id for group in roster.groups] == ["od-a", "od-b"]
    assert [employee.lark_id for employee in roster.employees] == ["alice", "bob"]
    assert roster.employees[0].name == "Alice"
    assert transport.calls[2]["params"]["page_token"] == "next-dept"
    assert transport.calls[4]["params"]["page_token"] == "next-user"


def test_lark_api_client_raises_on_lark_error() -> None:
    transport = FakeTransport([{"code": 999, "msg": "bad token"}])
    client = LarkApiClient("app_id", "app_secret", transport=transport)

    try:
        client.tenant_access_token()
    except RuntimeError as exc:
        assert "bad token" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected lark error")


def test_lark_api_client_raises_when_token_missing() -> None:
    transport = FakeTransport([{"code": 0}])
    client = LarkApiClient("app_id", "app_secret", transport=transport)

    with pytest.raises(RuntimeError, match="did not include a token"):
        client.tenant_access_token()


def test_lark_api_client_rejects_authenticated_request_without_headers() -> None:
    client = LarkApiClient("app_id", "app_secret", transport=FakeTransport([]))

    with pytest.raises(ValueError, match="require headers"):
        client._request("GET", "/contact/v3/x")  # noqa: SLF001


def test_lark_roster_source_rejects_bad_pagination_shapes() -> None:
    missing_data = LarkRosterSource(
        LarkApiClient(
            "app_id",
            "app_secret",
            transport=FakeTransport(
                [
                    {"code": 0, "tenant_access_token": "token"},
                    {"code": 0},
                ]
            ),
        )
    )

    with pytest.raises(RuntimeError, match="did not include data"):
        missing_data.fetch_roster()

    bad_items = LarkRosterSource(
        LarkApiClient(
            "app_id",
            "app_secret",
            transport=FakeTransport(
                [
                    {"code": 0, "tenant_access_token": "token"},
                    {"code": 0, "data": {"items": "not-list"}},
                ]
            ),
        )
    )

    with pytest.raises(RuntimeError, match="data.items is not a list"):
        bad_items.fetch_roster()

    missing_page_token = LarkRosterSource(
        LarkApiClient(
            "app_id",
            "app_secret",
            transport=FakeTransport(
                [
                    {"code": 0, "tenant_access_token": "token"},
                    {"code": 0, "data": {"has_more": True, "items": []}},
                ]
            ),
        )
    )

    with pytest.raises(RuntimeError, match="page_token is missing"):
        missing_page_token.fetch_roster()


def test_lark_roster_source_maps_fallback_fields() -> None:
    transport = FakeTransport(
        [
            {"code": 0, "tenant_access_token": "token"},
            {"code": 0, "data": {"has_more": False, "items": [{"open_department_id": "od-a", "name": "A"}]}},
            {
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [
                        {
                            "union_id": "alice",
                            "name": "Alice",
                            "email": "alice@example.com",
                            "orders": [
                                {"department_id": "od-a", "department_order": 20},
                                {"department_id": "od-b", "department_order": 5},
                            ],
                            "custom_attrs": [
                                {"id": "github_attr", "value": {"url": "alice-gh"}}
                            ],
                        },
                        {
                            "union_id": "bob",
                            "name": "Bob",
                            "department_ids": ["od-a"],
                            "custom_attrs": [
                                {"id": "github_attr", "value": "bad-value"}
                            ],
                        },
                    ],
                },
            },
        ]
    )
    source = LarkRosterSource(
        LarkApiClient("app_id", "app_secret", transport=transport),
        github_custom_attr_id="github_attr",
    )

    roster = source.fetch_roster()

    assert roster.employees[0].email == "alice@example.com"
    assert roster.employees[0].github_id == "alice-gh"
    assert roster.employees[0].group_lark_id == "od-a"
    assert roster.employees[1].github_id is None
    assert roster.employees[1].group_lark_id == "od-a"


def test_lark_roster_source_requires_ids_and_names() -> None:
    source = LarkRosterSource(
        LarkApiClient(
            "app_id",
            "app_secret",
            transport=FakeTransport(
                [
                    {"code": 0, "tenant_access_token": "token"},
                    {"code": 0, "data": {"has_more": False, "items": [{"name": "No ID"}]}},
                ]
            ),
        )
    )

    with pytest.raises(RuntimeError, match="open_department_id"):
        source.fetch_roster()


def test_urllib_lark_transport_encodes_request(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return None

        def read(self):
            return b'{"code": 0, "data": {"ok": true}}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(lark.request, "urlopen", fake_urlopen)

    response = UrllibLarkTransport(base_url="https://example.test", timeout_seconds=7).request_json(
        "POST",
        "/x",
        params={"a": "1", "skip": None},
        json_body={"hello": "world"},
        headers={"Authorization": "Bearer token"},
    )

    assert response == {"code": 0, "data": {"ok": True}}
    assert captured["url"] == "https://example.test/x?a=1"
    assert captured["method"] == "POST"
    assert captured["body"] == b'{"hello": "world"}'
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["timeout"] == 7


def test_urllib_lark_transport_surfaces_http_error_body(monkeypatch) -> None:
    class FakeErrorResponse:
        def read(self):
            return b'{"code":40004,"msg":"no dept authority error"}'

        def close(self):
            return None

    def fake_urlopen(_req, timeout):
        assert timeout == 30
        raise urllib.error.HTTPError(
            url="https://example.test/x",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=FakeErrorResponse(),
        )

    monkeypatch.setattr(lark.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="no dept authority error.*40004"):
        UrllibLarkTransport(base_url="https://example.test").request_json("GET", "/x")
