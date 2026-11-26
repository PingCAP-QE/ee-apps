"""Budget endpoints must work on the production accounts schema, without account_id."""
from datetime import date

import pytest
from sqlalchemy import text

from ci_dashboard.api.queries import cost
from test_routes import _insert_cost_attribution, _insert_cost_budget, _insert_cost_source
from test_routes import api_client as _api_client

api_client = _api_client


def test_budget_fixture_matches_production_membership_columns(sqlite_engine):
    with sqlite_engine.connect() as c:
        columns = {r[1] for r in c.execute(text("PRAGMA table_info(cost_budgets)"))}
    assert {"accounts", "projects", "platform", "team", "scope_key", "cost_basis"} <= columns
    assert "account_id" not in columns


def test_home_weekly_account_summary_uses_accounts_schema(sqlite_engine, api_client):
    _insert_cost_source(sqlite_engine, vendor="aws", account_id="a", display_name="A")
    _insert_cost_budget(sqlite_engine, vendor="aws", account_id="a",
                        period_start_date="2026-01-01", period_end_date="2026-12-31",
                        budget_amount=36500)
    _insert_cost_attribution(sqlite_engine, usage_date="2026-11-10", vendor="aws",
                             account_id="a", repo="tidb", group_id=None,
                             net_cost=50, list_cost=50, dimension_hash="a-nov10")
    response = api_client.get("/api/v1/pages/cost-weekly-account-summaries", params={
        "start_date": "2026-11-09", "end_date": "2026-11-15", "granularity": "week",
    })
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["net_cost"] == 50
    assert item["period_budget"] == 36500
    assert item["weekly_budget"] == 700


def test_shared_budget_not_duplicated_into_each_account(sqlite_engine, api_client):
    for account in ["a", "b"]:
        _insert_cost_source(sqlite_engine, vendor="aws", account_id=account, display_name=account)
    _insert_cost_budget(sqlite_engine, vendor="aws", account_id="a", budget_name="shared",
                        period_start_date="2026-01-01", period_end_date="2026-12-31",
                        budget_amount=36500)
    with sqlite_engine.begin() as c:
        c.execute(text("UPDATE cost_budgets SET accounts='[\"a\",\"b\"]'"))
    response = api_client.get("/api/v1/pages/cost-weekly-account-summaries", params={
        "start_date": "2026-11-09", "end_date": "2026-11-15", "granularity": "week",
    })
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        assert item["period_budget"] is None
        assert item["weekly_budget"] is None


def test_qa_shared_plan_counts_budget_once_and_spend_from_both_accounts(
    sqlite_engine, api_client, monkeypatch,
):
    monkeypatch.setattr(cost, "_today", lambda: date(2026, 11, 16))
    for account, amount in [("a", 10), ("b", 20)]:
        _insert_cost_source(sqlite_engine, vendor="aws", account_id=account,
                            display_name=account, purpose="QA")
        _insert_cost_attribution(sqlite_engine, usage_date="2026-11-10", vendor="aws",
                                 account_id=account, repo="tidb", group_id=None,
                                 net_cost=amount, list_cost=amount, dimension_hash=account)
    _insert_cost_budget(sqlite_engine, vendor="aws", account_id="a", budget_name="shared",
                        period_start_date="2026-01-01", period_end_date="2026-12-31",
                        budget_amount=36500)
    with sqlite_engine.begin() as c:
        c.execute(text("UPDATE cost_budgets SET accounts='[\"a\",\"b\"]'"))
    response = api_client.get("/api/v1/pages/weekly-cost")
    assert response.status_code == 200, response.text
    pace = response.json()["budget_pace"]
    assert pace["overall"]["period_budget"] == 700
    assert pace["overall"]["actual_list_cost"] == 30
    plans = [item for item in pace["projects"] if item["period_budget"] is not None]
    assert len(plans) == 1
    assert plans[0]["name"] == "shared"
    assert plans[0]["actual_list_cost"] == 30


@pytest.mark.parametrize("restriction", [
    {"projects": '["Alpha"]'}, {"repo": "tidb"}, {"group_id": 1},
    {"manager_id": 1}, {"label_filters": '{"project":"Alpha"}'},
])
def test_scoped_plan_is_not_a_whole_account_budget(sqlite_engine, restriction):
    _insert_cost_budget(sqlite_engine, vendor="aws", account_id="a",
                        period_start_date="2026-01-01", period_end_date="2026-12-31",
                        budget_amount=36500)
    key, value = next(iter(restriction.items()))
    with sqlite_engine.begin() as c:
        c.execute(text(f"UPDATE cost_budgets SET {key}=:value"), {"value": value})
        result = cost._budget_periods_by_account_for_window(
            c, start_date=date(2026, 11, 9), end_date=date(2026, 11, 15),
        )
    assert result == {}


@pytest.mark.parametrize("accounts", [None, "[]", "null", '"not-an-array"'])
def test_unassigned_or_invalid_memberships_do_not_create_account_budget(sqlite_engine, accounts):
    _insert_cost_budget(sqlite_engine, vendor="aws", account_id="a",
                        period_start_date="2026-01-01", period_end_date="2026-12-31",
                        budget_amount=36500)
    with sqlite_engine.begin() as c:
        c.execute(text("UPDATE cost_budgets SET accounts=:accounts"), {"accounts": accounts})
        result = cost._budget_periods_by_account_for_window(
            c, start_date=date(2026, 11, 9), end_date=date(2026, 11, 15),
        )
    assert result == {}
