from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib import error, parse, request

from roster.jobs.sync_roster import FetchedEmployee, FetchedGroup, FetchedRoster, RosterSource

LARK_OPENAPI_BASE_URL = "https://open.feishu.cn/open-apis"


class LarkTransport(Protocol):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class UrllibLarkTransport:
    base_url: str = LARK_OPENAPI_BASE_URL
    timeout_seconds: int = 30

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        query = ""
        if params:
            query = "?" + parse.urlencode(
                {key: value for key, value in params.items() if value is not None}
            )
        url = f"{self.base_url}{path}{query}"
        body = None
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        req = request.Request(url, data=body, method=method, headers=request_headers)
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:  # noqa: S310
                payload = response.read().decode("utf-8")
        except error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(_format_lark_http_error(exc.code, payload)) from exc
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise RuntimeError("Lark API response is not a JSON object")
        return parsed


@dataclass
class LarkApiClient:
    app_id: str
    app_secret: str
    transport: LarkTransport | None = None

    def __post_init__(self) -> None:
        if self.transport is None:
            self.transport = UrllibLarkTransport()

    def tenant_access_token(self) -> str:
        response = self._request(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            json_body={"app_id": self.app_id, "app_secret": self.app_secret},
            auth=False,
        )
        token = response.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Lark tenant_access_token response did not include a token")
        return token

    def get(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
        token: str,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            path,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        if auth and headers is None:
            raise ValueError("authenticated Lark requests require headers")
        assert self.transport is not None
        response = self.transport.request_json(
            method,
            path,
            params=params,
            json_body=json_body,
            headers=headers,
        )
        code = response.get("code")
        if code != 0:
            message = response.get("msg") or response.get("message") or "unknown Lark API error"
            raise RuntimeError(f"Lark API {method} {path} failed: {message} (code={code})")
        return response


@dataclass(frozen=True)
class LarkRosterSource(RosterSource):
    client: LarkApiClient
    root_department_id: str = "0"
    github_custom_attr_id: str | None = None
    page_size: int = 50

    def fetch_roster(self) -> FetchedRoster:
        token = self.client.tenant_access_token()
        groups = self._fetch_groups(token)
        employees = self._fetch_employees(token, groups)
        return FetchedRoster(groups=groups, employees=employees)

    def _fetch_groups(self, token: str) -> list[FetchedGroup]:
        items = self._paginate(
            token,
            f"/contact/v3/departments/{self.root_department_id}/children",
            params={
                "user_id_type": "union_id",
                "department_id_type": "open_department_id",
                "fetch_child": True,
                "page_size": self.page_size,
            },
        )
        return [self._map_group(item) for item in items]

    def _fetch_employees(self, token: str, groups: list[FetchedGroup]) -> list[FetchedEmployee]:
        employees_by_lark_id: dict[str, FetchedEmployee] = {}
        for group in groups:
            items = self._paginate(
                token,
                "/contact/v3/users/find_by_department",
                params={
                    "user_id_type": "union_id",
                    "department_id_type": "open_department_id",
                    "department_id": group.lark_group_id,
                    "page_size": self.page_size,
                },
            )
            for item in items:
                employee = self._map_employee(item, fallback_group_id=group.lark_group_id)
                employees_by_lark_id.setdefault(employee.lark_id, employee)
        return list(employees_by_lark_id.values())

    def _paginate(
        self,
        token: str,
        path: str,
        *,
        params: dict[str, object],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            request_params = dict(params)
            if page_token:
                request_params["page_token"] = page_token
            response = self.client.get(path, params=request_params, token=token)
            data = response.get("data")
            if not isinstance(data, dict):
                raise RuntimeError(f"Lark API {path} response did not include data")
            page_items = data.get("items") or []
            if not isinstance(page_items, list):
                raise RuntimeError(f"Lark API {path} data.items is not a list")
            items.extend(item for item in page_items if isinstance(item, dict))
            if not data.get("has_more"):
                return items
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token:
                raise RuntimeError(f"Lark API {path} has_more=true but page_token is missing")
            page_token = next_token

    def _map_group(self, item: dict[str, Any]) -> FetchedGroup:
        group_id = _required_text(item, "open_department_id")
        parent_id = _optional_text(item.get("parent_department_id"))
        return FetchedGroup(
            lark_group_id=group_id,
            name=_required_text(item, "name"),
            parent_lark_group_id=None if parent_id in {None, "0"} else parent_id,
            manager_lark_id=_optional_text(item.get("leader_user_id")),
        )

    def _map_employee(self, item: dict[str, Any], *, fallback_group_id: str) -> FetchedEmployee:
        lark_id = _required_text(item, "union_id")
        return FetchedEmployee(
            lark_id=lark_id,
            name=_required_text(item, "name"),
            employee_no=_optional_text(item.get("employee_no")),
            email=_optional_text(item.get("enterprise_email") or item.get("email")),
            github_id=self._github_id_from_custom_attrs(item),
            join_time=_join_time_from_epoch(item.get("join_time")),
            manager_lark_id=_optional_text(item.get("leader_user_id")),
            group_lark_id=_primary_group_id(item, fallback_group_id=fallback_group_id),
        )

    def _github_id_from_custom_attrs(self, item: dict[str, Any]) -> str | None:
        if not self.github_custom_attr_id:
            return None
        custom_attrs = item.get("custom_attrs")
        if not isinstance(custom_attrs, list):
            return None
        for attr in custom_attrs:
            if not isinstance(attr, dict) or attr.get("id") != self.github_custom_attr_id:
                continue
            value = attr.get("value")
            if not isinstance(value, dict):
                return None
            return _optional_text(
                value.get("text")
                or value.get("url")
                or value.get("option_value")
                or value.get("name")
            )
        return None


def _primary_group_id(item: dict[str, Any], *, fallback_group_id: str) -> str:
    orders = item.get("orders")
    if isinstance(orders, list):
        order_items = [order for order in orders if isinstance(order, dict)]
        for order in order_items:
            if order.get("is_primary_dept"):
                primary = _optional_text(order.get("department_id"))
                if primary:
                    return primary
        sorted_orders = sorted(
            order_items,
            key=lambda order: int(order.get("department_order") or 0),
            reverse=True,
        )
        if sorted_orders:
            primary = _optional_text(sorted_orders[0].get("department_id"))
            if primary:
                return primary
    department_ids = item.get("department_ids")
    if isinstance(department_ids, list):
        for department_id in department_ids:
            normalized = _optional_text(department_id)
            if normalized:
                return normalized
    return fallback_group_id


def _required_text(item: dict[str, Any], key: str) -> str:
    value = _optional_text(item.get(key))
    if value is None:
        raise RuntimeError(f"Lark response item missing required field {key!r}")
    return value


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _join_time_from_epoch(value: object) -> datetime | None:
    if not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)


def _format_lark_http_error(status_code: int, payload: str) -> str:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return f"Lark API HTTP {status_code}: {payload[:500]}"
    if not isinstance(parsed, dict):
        return f"Lark API HTTP {status_code}: {payload[:500]}"
    message = parsed.get("msg") or parsed.get("message") or "unknown Lark API error"
    code = parsed.get("code")
    return f"Lark API HTTP {status_code}: {message} (code={code})"
