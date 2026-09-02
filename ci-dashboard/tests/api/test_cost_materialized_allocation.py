from datetime import date

from sqlalchemy import create_engine, event, text

from ci_dashboard.api.queries.base import CommonFilters
from ci_dashboard.api.queries.cost import get_cost_trend

def test_cost_trend_reads_native_attribution_only() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE cost_attribution_daily (
              usage_date TEXT, vendor TEXT, account_id TEXT, target_branch TEXT,
              sku_name TEXT, list_cost REAL, effective_cost REAL, net_cost REAL,
              attribution_status TEXT
            )
        """))
        connection.execute(text("""
            CREATE TABLE cost_budgets (
              vendor TEXT, account_id TEXT, period_start_date TEXT,
              period_end_date TEXT, group_id INTEGER, manager_id INTEGER,
              repo TEXT, label_filters TEXT, budget_amount REAL
            )
        """))
        connection.execute(text("""
            INSERT INTO cost_attribution_daily VALUES
              ('2026-08-10', 'gcp', 'project-1', NULL, 'sku', 10, 10, 10, 'matched')
        """))

    result = get_cost_trend(engine, CommonFilters(
        start_date=date(2026, 8, 10), end_date=date(2026, 8, 10),
        granularity="week", cost_vendor="gcp", cost_account_id="project-1",
    ))

    assert result["meta"]["allocation_basis"] == "current_attribution"
    assert result["meta"]["summary"]["net_cost"] == 10


def test_cost_trend_aggregates_resource_coverage_with_trend_rows() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE cost_attribution_daily (
              usage_date TEXT, vendor TEXT, account_id TEXT, target_branch TEXT,
              sku_name TEXT, list_cost REAL, effective_cost REAL, net_cost REAL,
              attribution_status TEXT
            )
        """))
        connection.execute(text("""
            CREATE TABLE cost_budgets (
              vendor TEXT, account_id TEXT, period_start_date TEXT,
              period_end_date TEXT, group_id INTEGER, manager_id INTEGER,
              repo TEXT, label_filters TEXT, budget_amount REAL
            )
        """))
        connection.execute(
            text("""
                INSERT INTO cost_attribution_daily
                  (usage_date, vendor, account_id, sku_name, list_cost,
                   effective_cost, net_cost, attribution_status)
                VALUES
                  (:usage_date, 'gcp', 'project-1', :sku_name, :list_cost,
                   :effective_cost, :net_cost, :attribution_status)
            """),
            [
                {
                    "usage_date": "2026-08-10", "sku_name": "regular", "list_cost": 10,
                    "effective_cost": 9, "net_cost": 8, "attribution_status": "matched",
                },
                {
                    "usage_date": "2026-08-10", "sku_name": "regular", "list_cost": 20,
                    "effective_cost": 18, "net_cost": 16, "attribution_status": "unmatched",
                },
                {
                    "usage_date": "2026-08-10", "sku_name": "regular", "list_cost": None,
                    "effective_cost": None, "net_cost": None, "attribution_status": "matched",
                },
                {
                    "usage_date": "2026-08-10",
                    "sku_name": "Compute Flexible Committed Use Discounts - 3 Year",
                    "list_cost": 100, "effective_cost": 0, "net_cost": 0,
                    "attribution_status": "matched",
                },
                {
                    "usage_date": "2026-08-17", "sku_name": "regular", "list_cost": 30,
                    "effective_cost": 27, "net_cost": 24, "attribution_status": "matched",
                },
                {
                    "usage_date": "2026-08-17", "sku_name": "regular", "list_cost": 40,
                    "effective_cost": 36, "net_cost": 32, "attribution_status": "unmatched",
                },
                {
                    "usage_date": "2026-08-10", "sku_name": "regular", "list_cost": 0.004,
                    "effective_cost": 0.004, "net_cost": 0.004, "attribution_status": "matched",
                },
                {
                    "usage_date": "2026-08-17", "sku_name": "regular", "list_cost": 0.004,
                    "effective_cost": 0.004, "net_cost": 0.004, "attribution_status": "matched",
                },
            ],
        )

    attribution_reads: list[str] = []

    def record_attribution_read(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "cost_attribution_daily" in statement.lower():
            attribution_reads.append(statement)

    event.listen(engine, "before_cursor_execute", record_attribution_read)
    try:
        result = get_cost_trend(
            engine,
            CommonFilters(
                start_date=date(2026, 8, 10), end_date=date(2026, 8, 23),
                granularity="week", cost_vendor="gcp", cost_account_id="project-1",
            ),
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_attribution_read)

    summary = result["meta"]["summary"]
    assert summary["net_cost"] == 80.0
    assert summary["effective_cost"] == 90.0
    assert summary["list_cost"] == 100.0
    assert summary["total_resource_cost"] == 100.01
    assert summary["matched_resource_cost"] == 40.01
    assert summary["matched_resource_pct"] == 40.01
    assert len(attribution_reads) == 1
    assert {series["key"]: series["points"] for series in result["series"]}["list_cost"] == [
        ["2026-08-10", 30.0],
        ["2026-08-17", 70.0],
    ]
