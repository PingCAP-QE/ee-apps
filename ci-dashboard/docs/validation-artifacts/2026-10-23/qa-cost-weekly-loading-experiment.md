# QA Cost Weekly loading experiment

Captured: 2026-10-23 UTC. **Read-only investigation; no QA application, frontend,
test, schema, deployment, or cluster configuration changes.**

## Decision

**A summary-first split alone is not worthwhile for the first useful chart.** It
removes only about **1.05–1.23 s** from the measured initial report, mostly
allocation SQL/network time, and then makes the weekly charts wait for another
request. The expensive part is the summary/account scan, not Python allocation.

**A minimal chart-first split is worth a follow-up prototype**, if the goal is
Budget pace/scenario and Team share charts rather than cards: reuse the existing
`allocation?period=week` endpoint independently of the summary response, and omit
allocation from that summary request. The isolated weekly-allocation call took
**2.15 s**, versus **7.13–7.61 s** for the current combined first report. That is
an opportunity to remove the summary dependency from useful charts, **not a
measured concurrent/browser/deployment speedup**. Keep QA unchanged in this
iteration; validate this precise chart-first variant before shipping it, rather
than adding endpoints for every chart.

The **top cumulative budget-period chart is a separate case**: it comes from
`/trend`, which first executes eight-week history and then budget-period actuals.
Neither summary cards nor a weekly-allocation response contains that chart.

## Serving identity and scope

The configured local connection was independently compared with a running app
pod; credentials and the DB hostname were not printed.

| property | local experiment | running pod |
| --- | --- | --- |
| host SHA-256, first 16 hex characters | `306311649d766b38` | `306311649d766b38` |
| port / schema | `4000` / `insight` | `4000` / `insight` |
| TLS CA configured | yes | yes |
| `VERSION()` | `8.0.11-TiDB-v8.5.3-serverless` | same |
| `CURRENT_DATE()` | `2026-10-23` | same |
| experiment session SQL deadline | `6000` ms | identity-check session also `6000` ms |

Runtime check: existing context
`gke_pingcap-testing-account_us-central1-c_prow`, namespace `apps`, deployment
`ci-dashboard`, pod `ci-dashboard-88574c8cb-cx7ll`. Deployment image:
`ghcr.io/pingcap-qe/ee-apps/ci-dashboard:v2026.10.18-2-g0335d2a`.
`kubectl get` and a read-only `exec` calculated the host fingerprint in the pod
and ran only the identity SELECT. No login, credential refresh, context switch,
secret dump, resource mutation, or deployment was performed.

This verifies the sampled pod's configured host/schema against the local
connection. It does not establish identical query plans, network latency,
application revision, or configuration on every replica. **Timings below run
current worktree Python locally against that DB, not the deployed HTTP handler.**
The imported module was explicitly
`ee-apps.cost-refine/ci-dashboard/src/ci_dashboard/api/queries/cost.py`.
SHA-256 of `inspect.getsource(get_weekly_cost_report)` at measurement:
`f63ee168cc9ebd681978448e54ffbd0bbf72a89c44aac367b56ff7e0cbed0c2b`.

Fixed report date: `2026-10-23` UTC. Periods:

- last complete week: October 12–18; preceding week: October 5–11;
- previous natural month: September 1–30;
- combined summary SQL scan: September 1–October 18, inclusive;
- eight-week history: August 24–October 18;
- current-month allocation: October 1–31, **not** October 1–23.

Current code selects active sources with `category = 'QA'` when that column
exists, falling back to non-blank purpose on older schemas. This DB used the
category path, with **13 sources**; the purpose-only language in
`weekly-cost-design.md` is older than this code path. Source membership was
checked before/after the primary probes and matched every report; its canonical
sorted-pair JSON SHA-256 was
`99238d11803f422bae1508fbeaeb06e5ce76233da9dbaf33cbda640f6e6c2185`.
No source allowlist, accounting expression, or optimizer hint was substituted.

## Safety and method

Primary probes ran sequentially at **01:23:13–01:24:00 UTC**, using
`/Users/dillon/workspace/ee-apps/ci-dashboard/.venv/bin/python`, from this
worktree's `ci-dashboard/`, with `PYTHONPATH=src` and bytecode writes disabled.
Connection settings were read without displaying them from
`/Users/dillon/workspace/ee-apps/ci-dashboard/.local/tidb.env`.

- Client read/write deadline: 8 s; SQL `MAX_EXECUTION_TIME`: 6 s; connect: 5 s.
  These are per-statement/socket limits, not a six-second whole-report limit.
- One connection/pool path, no added query concurrency; no more than three
  samples of each request shape. Any failed heavy probe would stop later heavy
  probes. **No statement timed out or failed.**
- SQLAlchemy before/after-cursor events timed SQL; in-memory wrappers timed
  helpers. Nested helper durations are inclusive and must not be added twice.
  PyMySQL buffered fetch/network time is included; these are not pure server CPU
  or TiDB statement-summary measurements.
- Identity/metadata reads warmed the primary connection before report samples.
  No fact-table counts, writes, DDL, `EXPLAIN ANALYZE`, or load test were used.
- An in-memory boundary replacement skipped `_weekly_cost_allocation_inputs`
  and `_weekly_cost_allocation_response` only for summary-only simulations. All
  summary/account SQL and its result construction remained the native code.
  Captured allocation inputs were replayed through the original helper to check
  exact merged-response parity without another fact scan.

## Primary measurements

All durations in seconds. Three normal calls had identical complete JSON
responses (13 accounts, 14,394 bytes using the harness's JSON serialization).

| normal `include_trend=False` sample | overall | summary/accounts SQL | week dimension SQL | allocation input helper | allocation Python response | estimated overall without allocation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 7.613 | 5.518 | 0.433 | 1.060 | 0.0100 | 6.543 |
| 2 | 7.296 | 5.039 | 0.603 | 1.230 | 0.0038 | 6.063 |
| 3 | 7.134 | 5.059 | 0.416 | 1.045 | 0.0071 | 6.082 |

The final column is **within-sample subtraction**, not another request:
`overall - allocation_inputs - allocation_response`. Median normal overall was
7.296 s; median attributed allocation contribution was 1.070 s, about 15%.
Median summary SQL was 5.059 s, about 69% of normal overall. Python allocation
was only 4–10 ms.

Each normal call issued seven SQL statements:

| SQL component | observed range | rows returned |
| --- | ---: | ---: |
| category schema check | 0.204–0.205 | 1 |
| purpose schema check | 0.204–0.205 | 1 |
| summary/accounts | 5.039–5.518 | 13 |
| shared weekly allocation dimensions | 0.416–0.603 | 223 |
| active roster | 0.208–0.214 | 135 |
| budget-membership schema check | 0.203–0.205 | 1 |
| overlapping QA budgets | 0.210–0.212 | 6 |

The allocation metadata helper took 0.626–0.628 s in total. About 0.62 s per
request was outside the instrumented statements/Python allocation, including
connection validation/transaction round trips. The approximately 0.20 s floor
on tiny local SELECTs makes network distance material; do not project the full
local 1.07 s removal onto in-pod serving latency.

### Actual summary-only simulation and independent week allocation

| shape/sample | measured overall | summary SQL | exact summary/account parity | exact merged-response parity |
| --- | ---: | ---: | --- | --- |
| summary-only 1 | 6.609 | 5.581 | yes | yes |
| summary-only 2 | 5.316 | 4.292 | yes | yes |
| summary-only 3 | 5.586 | 4.560 | yes | yes |
| existing `allocation?period=week`, one sample | 2.148 | n/a | n/a | budget/team fields match normal report exactly |

The summary-only median was 5.586 s. **Do not attribute the entire 1.711 s
median difference to splitting:** the samples were not interleaved and summary
SQL itself varied. The more defensible attributable removal is the normal
samples' 1.052–1.234 s allocation contribution.

Each capture contained `(223 dimension rows, 135 roster rows, 6 budget rows)`.
Replaying the original allocation response took 3–8 ms, and merging it with the
summary-only response reproduced every JSON value, order, null and amount in
all three normal reports. The separate week endpoint also returned exactly the
same `budget_pace` and `team_share` objects on this data. This is sample parity,
not a universal guarantee: the existing week endpoint derives its total by
rounding the raw dimension grand total, whereas the combined report sums
rounded per-account amounts. A production split must preserve the existing
rounding/denominator contract, particularly for the legacy budget fallback.

## Bounded dependent work

Two current-month samples and one sample of each trend helper were subsequently
run sequentially. Active-budget metadata found six qualifying product plans,
with earliest start **September 1**, so the budget helper was explicitly gated
to a window of at most 60 days before execution; actual window was 53 days.

| shape | measured duration | fact SQL | returned grouping/points | qualification |
| --- | ---: | ---: | --- | --- |
| current-month allocation, first connection | 6.657 | 1.083 | 938 dimensions | includes cold connection setup; not comparable to warm serving |
| current-month allocation, prewarmed connection | 2.727 | 1.025 | 938 dimensions | same exact response hash as first sample |
| budget-period helper, Sep 1–Oct 23 | 3.661 | 3.226 | 2,809 dimensions; 8 weekly points | helper only, not endpoint |
| eight-week history helper, Aug 24–Oct 18 | 2.992 | 2.987 | 637 source/day groups; 13 series | helper only, not endpoint |

The cold current-month sample's SQL summed to 2.117 s and allocation Python
was 0.009 s; the remaining approximately 4.53 s includes connection and
transaction overhead. It is not evidence of a new slow allocation query.

The two trend helpers sum to **6.653 s**, excluding endpoint source/schema and
connection/transaction overhead. They were measured separately in reverse
order from the endpoint, so this is a component sum, **not an observed trend
endpoint latency**. Source shows the deployed-shaped endpoint serializes history
before budget-period cost; thus a slow lower history chart can also hold the
top cumulative chart. No additional full `/trend` request was generated here.

### Existing browser observations supplied by parent session

The parent inspected already-completed performance entries in an authenticated
`https://prow.tidb.net/dashboard/qa-cost-weekly` tab; it did not generate new
benchmark traffic. One completed request per shape showed:

| request | total entry duration | before `requestStart` | request-to-TTFB | transferred body size reported |
| --- | ---: | ---: | ---: | ---: |
| initial `include_trend=false` | 21.141 s | 15.211 s | 5.929 s | 13,387 bytes |
| trend | 6.371 s | not supplied | 6.360 s | 8,956 bytes |
| current-month allocation | 2.103 s | not supplied | 2.071 s | 20,840 bytes |

The 15.211 s before request dispatch is **not measured SQL time**, and its cause
was not established. These are individual existing browser observations, not
latency statistics or a controlled comparison with the local probe. Earlier
zero-byte requests aborted on route navigation were excluded. Do not compare
body sizes directly with the harness's differently serialized JSON.

## Loading alternatives: what would actually benefit?

Current source already does progressive loading: the initial report contains
summary/accounts **and** weekly budget/team allocations; after it resolves,
trend and current-month allocation start concurrently. The week Budget pace,
scenario allocation charts and Team share can render from the first response.
The cumulative chart has no points until trend completes, and
`CumulativeWeeklyCostChart` returns nothing while those points are absent.

| candidate | cards/accounts | weekly budget/team/scenario charts | top cumulative chart |
| --- | --- | --- | --- |
| unchanged | normal first response | normal first response | first response, then entire trend |
| summary-only first, then allocation | roughly 1.05–1.23 s attributable local saving | **not earlier**; estimated 6.08 + 2.15 = 8.23 s versus normal median 7.30 s | trend can start about one allocation contribution earlier, but still waits for both trend scans |
| summary-only and shared week allocation independently eligible at mount | summary path remains roughly 6.08 s by same-sample subtraction | isolated allocation path 2.15 s is the opportunity; **not tested concurrently** | unchanged unless its trend enablement is also moved off the summary gate |

The 6.08 s figure is the median within-sample summary estimate, not a deployed
or concurrent result. Query contention, HTTP scheduling, authentication,
serialization and rendering were not modeled. A fourth request at startup is
not automatically faster; do not launch every chart independently.

Minimum implementation worth validating later:

1. Keep default report compatibility, with an opt-in way to omit **all** weekly
   allocation work from the summary request. Reuse the existing week-allocation
   endpoint once; do not leave duplicate weekly dimension scans in both calls.
2. Let weekly allocation render Budget pace/scenario and Team share without
   `report.loading`/`report.error` gating them. Simply adding a fetch while
   retaining today's `allocationLoading = report.loading || ...` does not
   accelerate charts. Preserve old-schema/no-source states and fixed UTC dates.
3. Keep budget/team allocation together. They share one dimension scan, one
   roster/budget load and one `_weekly_cost_team_dimensions` pass. Projects and
   source/date/project values are required for plan membership, scoped actuals,
   proration, deduplicated budget unions and scenario daily allocations; owners
   are required for Owner share. Do not apply CI-only dimension trimming to QA.
4. Treat top cumulative loading separately. The smallest additional experiment
   there is prioritizing its existing budget-period helper independently of the
   lower eight-week history helper, which already uses a different SQL scan.
   It is not a reason to duplicate the allocation scan or widen its date range.
   Validate browser time-to-visible-chart and DB contention before adopting it.

No recommendation is made here for indexes, hints, rollups, monetary-source
substitution, or caches. The September 8 baseline measured the older full
endpoint and ten QA sources, so its 6.39 s p50 is historical context only, not a
before/after number for this experiment.

## Reproducing the principal experiment

Temporary scripts/results used for this run were outside the repository:
`/tmp/qa_weekly_loading_experiment.py`, `/tmp/qa_weekly_loading_followup.py`,
`/tmp/qa_weekly_loading_warm_current_month.py`, and corresponding
`/tmp/qa-weekly-loading-*-results.json` files. They are disposable; the essential
procedure is reproduced below. Do not increase deadlines or retry heavy probes
following a timeout. Run the optional dependent helpers only after checking
active budget bounds; never execute an unknown multi-year active window.

```python
# Save outside the repo, run from ci-dashboard:
# PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
# /Users/dillon/workspace/ee-apps/ci-dashboard/.venv/bin/python /tmp/qa-probe.py
import functools, json, os, shlex, time
from datetime import date
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import event, text
from ci_dashboard.api.queries import cost
from ci_dashboard.common.config import load_settings
from ci_dashboard.common.db import build_engine

env = dict(os.environ)
for line in Path('/Users/dillon/workspace/ee-apps/ci-dashboard/.local/tidb.env').read_text().splitlines():
    words = shlex.split(line, comments=True)
    if words[:1] == ['export']:
        words = words[1:]
    if len(words) == 1 and '=' in words[0]:
        key, value = words[0].split('=', 1)
        env[key] = value
# Never print env, settings, URLs, passwords, or full connection exceptions.
env.update(CI_DASHBOARD_DB_QUERY_TIMEOUT_SECONDS='6',
           CI_DASHBOARD_DB_READ_TIMEOUT_SECONDS='8',
           CI_DASHBOARD_DB_WRITE_TIMEOUT_SECONDS='8',
           CI_DASHBOARD_DB_CONNECT_TIMEOUT_SECONDS='5',
           CI_DASHBOARD_DB_POOL_SIZE='1', CI_DASHBOARD_DB_MAX_OVERFLOW='1')
engine = build_engine(load_settings(env))
cost._today = lambda: date(2026, 10, 23)
original_inputs = cost._weekly_cost_allocation_inputs
original_response = cost._weekly_cost_allocation_response
captured = []

@event.listens_for(engine, 'before_cursor_execute')
def before(c, cursor, sql, params, context, many):
    assert sql.lstrip().upper().startswith(('SELECT', 'SHOW'))
    context.started = time.perf_counter()

@event.listens_for(engine, 'after_cursor_execute')
def after(c, cursor, sql, params, context, many):
    print('sql', round(time.perf_counter() - context.started, 4), cursor.rowcount)

def timed(name, fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        start = time.perf_counter()
        value = fn(*args, **kwargs)
        print(name, round(time.perf_counter() - start, 4))
        if name == '_weekly_cost_allocation_inputs':
            captured.append(value)
        return value
    return wrapped

for name in ('_weekly_cost_allocation_inputs', '_weekly_cost_allocation_response',
             '_weekly_cost_allocation_dimension_rows',
             '_weekly_cost_allocation_metadata', '_weekly_cost_budget_rows',
             '_weekly_cost_team_dimensions'):
    setattr(cost, name, timed(name, getattr(cost, name)))

def measured(fn):
    start = time.perf_counter()
    try:
        value = fn()
    except Exception as exc:
        # Unhandled SystemExit stops every following heavy probe.
        raise SystemExit('STOP: ' + type(exc).__name__) from None
    print('overall', round(time.perf_counter() - start, 4))
    return value

try:
    with engine.connect() as c:
        # Warm connection; independently compare host/schema fingerprint first.
        c.execute(text('SELECT DATABASE(), VERSION(), CURRENT_DATE(), @@session.max_execution_time')).one()
        clause = cost._weekly_cost_qa_source_clause(c)
        sources = cost._weekly_cost_qa_sources(c, qa_source_clause=clause)
    normal = [measured(lambda: cost.get_weekly_cost_report(engine, include_trend=False))
              for _ in range(3)]
    assert all(x == normal[0] for x in normal)
    assert all({(i['vendor'], i['account_id']) for i in x['items']} == sources
               for x in normal)
    for index in range(3):
        with patch.object(cost, '_weekly_cost_allocation_inputs', lambda *a, **k: ((), (), ())), \
             patch.object(cost, '_weekly_cost_allocation_response',
                          lambda *a, **k: {'team_share': None, 'budget_pace': None}):
            summary = measured(lambda: cost.get_weekly_cost_report(engine, include_trend=False))
        summary.pop('team_share'); summary.pop('budget_pace')
        allocation = original_response(*captured[index], date(2026, 10, 12),
            date(2026, 10, 18), normal[index]['summary']['last_week_cost'], sources)
        assert {**summary, **allocation} == normal[index]
    separate = measured(lambda: cost.get_weekly_cost_allocation(engine, 'week'))
    assert all(separate[k] == normal[0][k] for k in ('team_share', 'budget_pace'))
    with engine.connect() as c:
        assert cost._weekly_cost_qa_sources(c, qa_source_clause=clause) == sources
finally:
    engine.dispose()
```

Dependent probes use the same configuration, instrumentation and frozen clock:
`get_weekly_cost_allocation(engine, 'current_month')` (two samples, one cold and
one prewarmed); `_weekly_cost_budget_period_cost(connection, date(2026,10,23),
sources, qa_source_clause=clause)` (one, after metadata safety gate); and
`_weekly_cost_list_cost_history(connection, items, date(2026,10,12),
date(2026,10,18), qa_source_clause=clause)` (one). Here `items` comes from
`_weekly_cost_active_source_items`; metadata gating uses
`_weekly_cost_product_budget_plans(_weekly_cost_budget_rows(...), sources,
today)` and asserts `(today - earliest_start).days <= 60`.

## Local regression check and source references

No test files were edited. Existing SQLite QA regressions passed:

```text
cd ci-dashboard
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /Users/dillon/workspace/ee-apps/ci-dashboard/.venv/bin/python -m pytest \
  tests/api/test_routes.py -k 'weekly_cost and not ci_weekly_cost' \
  --no-cov -o cache_dir=/tmp/qa-weekly-pytest-cache
17 passed, 111 deselected, 137 existing deprecation warnings in 0.76s
```

References:

- `src/ci_dashboard/api/queries/cost.py`: `get_weekly_cost_report`,
  `get_weekly_cost_allocation`, `get_weekly_cost_trend`,
  `_weekly_cost_allocation_inputs`, `_weekly_cost_allocation_response`,
  `_weekly_cost_team_dimensions`, `_weekly_cost_budget_period_cost`,
  `_weekly_cost_list_cost_history`, `_billing_report_list_cost_expr`.
- `src/ci_dashboard/api/queries/pages.py` and `api/routes/pages.py`: thin QA
  route delegates; no separate hidden report cache was used.
- `web/src/pages/WeeklyCostPage.jsx`: request eligibility, allocation state,
  Budget pace/Team share gates, trend dependency and cumulative item source.
- `web/src/components/WeeklyCostBudget.jsx`: cumulative renderer returns null
  without points; `web/src/components/charts.jsx`: `Panel` hides children while
  loading or errored; `web/src/lib/api.js`: enabled request behavior.
- `docs/weekly-cost-design.md`: fixed UTC windows, list-cost expression,
  progressive trend contract, budget/team semantics; monetary helpers retain
  CNY conversion at 6.5 and the GCP flexible-CUD exclusion.
- `docs/validation-artifacts/2026-09-08/qa-cost-weekly-prod-baseline.md`:
  historical full-endpoint benchmark only.
- `cost-insight/sql/009_add_cost_serving_indexes.sql`: existing source/date
  index migration; no index/schema change is part of this experiment.
