from datetime import date

from sqlalchemy import create_engine, text

from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.cost import get_cost_trend


def test_cost_trend_selects_the_published_materialized_perspective() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE cost_attribution_daily (
                  usage_date TEXT, vendor TEXT, account_id TEXT, target_branch TEXT,
                  sku_name TEXT, list_cost REAL, effective_cost REAL, net_cost REAL,
                  attribution_status TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE cost_allocation_daily (
                  basis_key TEXT, allocation_version TEXT, usage_date TEXT,
                  vendor TEXT, account_id TEXT, target_branch TEXT, sku_name TEXT,
                  list_cost REAL, effective_cost REAL, net_cost REAL,
                  attribution_status TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE cost_allocation_publication (
                  publication_name TEXT PRIMARY KEY, active_allocation_version TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE cost_budgets (
                  vendor TEXT, account_id TEXT, period_start_date TEXT,
                  period_end_date TEXT, group_id INTEGER, manager_id INTEGER,
                  repo TEXT, label_filters TEXT, budget_amount REAL
                )
                """
            )
        )
        for statement in (
            """
            INSERT INTO cost_attribution_daily VALUES
              ('2026-08-10', 'gcp', 'project-1', NULL, 'sku', 10, 10, 10, 'matched')
            """,
            """
            INSERT INTO cost_allocation_daily VALUES
              ('eq_allocated', 'v1', '2026-08-10', 'gcp', 'project-1', NULL,
               'sku', 10, 10, 7, 'matched')
            """,
            "INSERT INTO cost_allocation_publication VALUES ('dashboard', 'v1')",
        ):
            connection.execute(text(statement))

    result = get_cost_trend(
        engine,
        CommonFilters(
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            granularity="week",
            cost_vendor="gcp",
            cost_account_id="project-1",
        ),
        allocation_basis="eq_allocated",
    )

    assert result["meta"]["allocation_basis"] == "eq_allocated"
    assert result["meta"]["summary"]["net_cost"] == 7
