# Flaky dashboard: zero values and filter round trips (2026-11-24)

## Verified symptoms

For the UTC week 2026-11-16 through 2026-11-22, successful live API responses,
rendered cards, and production database queries agree:

| Repository | Branch | Flaky / failure-like builds | Rate |
| --- | --- | --- | --- |
| pingcap/tidb | release-nextgen-20251011 | 0 / 17 | 0.00% |
| pingcap/tidb | release-nextgen-202603 | 17 / 108 | 15.74% |
| pingcap/tidb | release-nextgen-202609 | 2 / 83 | 2.41% |
| tikv/pd | release-nextgen-202603 | 2 / 12 | 16.67% |

`issue_status=closed` does not affect these build-scoped rates. Case-level evidence
is a separate metric: TiDB/release-nextgen-20251011 has three distinct reported
flaky cases even though its retry-recovery build rate is zero.

## Presentation root causes and fixes

- `useApiData` retains prior data during refetch. The Flaky cards previously
  displayed that data under the newly selected branch without checking its
  `responseKey`. The page now treats a response-key mismatch as loading.
- On request failure, the hook clears its data. Unconditional `|| 0` defaults in
  the cards turned an unavailable result into four zero percentages. Cards now
  render a loading placeholder or the actual request error, not a numeric value.
- Successful queries without a denominator now display `N/A`; an observed zero
  numerator with a positive denominator still displays `0.0%`. Comparisons require
  positive denominators in both periods.
- All selections use empty strings. URL serialization previously omitted them,
  and parsing restored the Flaky defaults (`pingcap/tidb`, `master`, `closed`).
  Empty selections that differ from non-empty defaults are now explicit URL
  parameters, such as `issue_status=`. API requests still omit empty filters.

## Verification

Regression tests cover loading, filter changes, HTTP 504, positive-denominator
zero rates versus missing denominators, and URL/navigation round trips for All
repo/branch/issue selections. The App-level test verifies the real select handler
and that All issues does not send `issue_status=closed` to the API.

A local browser check with intercepted fixture responses also verified:

1. All issues remains selected after reload, with `issue_status=` in the page
   URL and no issue-status filter in the API request.
2. A held next-branch response shows `Loading metric...`, not the previous rate.
3. An injected HTTP 504 shows `Could not load metric: 504 Gateway Timeout`, not 0%.

Browser-injected failures are test fixtures, not production incidents. Neither
this reproduction nor the real 504 proves which failure affected the historical
weekly report.

## GitHub Actions testcase coverage

CSE's flaky case reports use GitHub Actions run/attempt URLs. The old testcase
identity helper rejected those URLs, so Top cases silently discarded their
records even without a job filter. The fix accepts only validated GitHub HTTPS
workflow-run URLs in the report-time Top cases path, deduplicates retry attempts
by run ID, preserves run links, and does not infer runner cloud or execution
counts. Existing derived `TENCENT` fallbacks are overridden to `UNKNOWN` for
Actions reports. Explicit job or GCP/TENCENT filters exclude these unattributable
reports. Build ingestion and build-time weekly/rate semantics are unchanged;
the page now states this coverage boundary explicitly.

The patched query was run read-only against the serving database for all three
CSE branches in the report week. Each returned ten ranked cases. For example,
`cloud-engine`'s highest case affected 113 distinct workflow runs. No production
data or deployment was changed.

## Real 504: DB read-response timeout, not demonstrated slow SQL

Evidence collected directly from the live workload and serving database:

- Image: `ci-dashboard:v2026.11.15-2-gf94a5e0`, display version `1.9.22`.
- At approximately `2026-11-24 01:27:02 UTC`, the browser received a 504 after
  about 15 seconds. The live Envoy route for `/dashboard` has no explicit timeout,
  so Envoy's 15-second route default applies. No timeout configuration was changed.
- At `01:27:24.548 UTC`, pod `ci-dashboard-55bd969d78-fzrq2` logged
  `pymysql.err.OperationalError: (2013, 'Lost connection to MySQL server during
  query (The read operation timed out)')`. The traceback points to
  `get_issue_filtered_weekly_case_rates` -> `_fetch_issue_weekly_rate_rows`, not
  the build-rate calculation. The driver was waiting for the first result packet.
- Live settings are DB read timeout **35 s**, statement limit **30 s**, pool wait
  **5 s**, with `pool_pre_ping=True`. Connection checkout ping does not protect a
  query from a connection/response-path stall after checkout.
- The actual serving database is `insight` on the TiDB Cloud AWS us-west-2 gateway;
  it matches the database used for the local read-only validation.
- `information_schema.STATEMENTS_SUMMARY_HISTORY` contains the matching Issue
  weekly query digest `ea1f5dd73acfa1dea1a830cfc3068feff1d3455bad2b1fc9875b448a2753cbd0`.
  In its `01:26:25–01:27:26` interval there are two executions, **0 SQL errors**,
  maximum **1.423 s**, average **1.039 s**, last seen `01:26:49`. The adjacent
  `01:27:26–01:28:27` interval has three executions, maximum **0.720 s**, no errors.
  The slowest statement of any digest in the inspected fault intervals was
  **1.467 s**. The database records do not show a 30–35 second SQL execution.
- Subsequent successful requests at `01:27:45` and `01:28:11` returned the correct
  nonzero rates. Both application pods remained running with zero restarts.

The established failure chain is: DB result-read/connection-path stall -> one
Issue weekly section waits past the gateway deadline -> entire aggregate page
gets 504 -> the old UI rendered missing data as zero. The socket read timeout is
real, but its wording alone does not prove a server-initiated disconnect. The
statement-history evidence argues against slow SQL as the cause of this specific
incident. It cannot identify whether the response was lost/stalled in the TiDB
Cloud gateway, the network, or the client connection; statement summaries also
lack an end-to-end request/connection ID.

Do not add indexes, inflate gateway timeouts, or blindly retry this read after
35 seconds based on this incident. A production connection-layer investigation
needs time-correlated TiDB Cloud/network telemetry and request/connection IDs.
The frontend fix prevents the transport failure from masquerading as a valid 0%.
This change does **not** claim to repair the external connection stall.

## Final local checks

- Frontend: 97 tests passed; production bundle built successfully. This includes
  a synchronous bucket-ranking scope-change check and Cost date-clearing
  regressions; explicit-empty URL preservation is restricted to Flaky defaults.
- Backend: 1,830 tests passed. Four existing pod-watcher shell-renderer tests
  fail newline/tab validation on this machine; the identical failures were
  reproduced on unmodified main before this change. They are outside this fix.
- Focused Top cases/build-URL tests: 58 passed, including direct URL trust-boundary
  unit cases and a regression guard for both SQLite and production MySQL/TiDB
  expression generation. The new Python
  Actions URL normalizer has 100% statement/branch coverage in these focused
  tests; the full matcher module has 92.24% statement coverage and 85.29% branch
  coverage (89.67% combined). Changed Python files pass Ruff;
  `git diff --check` is clean.
- Five normalization cases were also executed as SELECT-only expressions on
  TiDB to verify that its regexp implementation agrees with the Python/SQLite
  helper, including malformed URLs and traversal-like path components.
- No application deployment, database writes, schema changes, or timeout
  configuration changes were made.
