# CI Cost Weekly progressive-loading validation

Captured: 2026-10-23 UTC. These changes are validated locally, not deployed.

## Existing-page baseline

An authenticated browser GET to the live
`https://prow.tidb.net/dashboard/api/v1/pages/ci-weekly-cost` returned HTTP 200
in 5.438 seconds (resource duration 5.428 seconds, request-to-first-byte about
5.414 seconds). Its JSON was about 10.8 KB. The existing page waits for this
single complete response before displaying any panel.

This is one browser sample, not a latency distribution or a new-version
benchmark. An older navigation entry took 19.58 seconds, but about 14.7 seconds
preceded request dispatch; that entire duration cannot be attributed to SQL.

## Read-only SQL investigation

The existing local TiDB config resolved to schema `insight`, TiDB v8.5.3,
with TLS enabled. The config was not independently compared with live pod
secrets. Probes were sequential, time-bounded SELECTs and EXPLAIN without
ANALYZE; no schema or data changes were made. Statement-summary access was
unavailable on this serverless instance.

For the complete week 2026-10-12 through 2026-10-18:

| Query | Local elapsed | Result rows |
| --- | ---: | ---: |
| Daily list/net values, limited to this week | 0.516 s | 14 |
| Original team/repository aggregation | 1.050 s | 1,702 |
| Team/repository aggregation without owner/project grouping | 0.526 s | 330 |
| Billing categories | 0.672 s | 133 |

These are single warm samples including approximately 0.21 seconds of network
RTT, not statistical benchmarks. Both share-query result sets were processed
through the real roster mapping and share helpers: complete team and repo panel
outputs, totals, ordering, percentages and Others matched exactly.

The patch applies only the measured owner/project grouping reduction. NULL
owner/project placeholders retain compatibility with the shared dimension
helper; the original source/date filters and list-cost expression remain.

The account/date index already exists and the native table has an available
TiFlash replica. No new index or forced storage-engine hint is justified by
these measurements. The full overview window (2026-08-24 through 2026-10-23)
was inspected with EXPLAIN only, not executed for this probe: its optimizer
estimate covers millions of rows. Full-window query latency is not established
by the one-week samples above.

Existing billing-summary/resource-serving tables are not safe drop-in rollups:
the billing summary is not necessarily smaller and does not reproduce native
attribution semantics; resource publication has list totals only, lacks net
cost and has freshness/completeness gaps. The active-plan cumulative chart
still requires plan-start-to-today data; truncating it to eight weeks would
change its amounts.

## Regression validation

- Deferred-response frontend tests verify overview/history become visible while
  lower requests remain pending, then share and categories load sequentially.
- Errors in each section remain local and do not prevent subsequent requests.
- Backend tests verify sections skip unrelated queries, reject invalid section
  values, and retain the default full-report contract and accounting amounts.
- Share regression covers owner/project subdivisions, both fixed accounts,
  CNY conversion, FCD exclusion, NULL/negative/cancelling amounts, missing repos,
  missing/outside-root teams, and excluded accounts/dates.
- Browser verification used live response data with deliberately held local
  share/category responses. Top charts rendered before either lower panel;
  releasing each response updated only its respective panel.

## Post-deployment follow-up

Repeat the authenticated browser measurement for `section=overview`,
`section=share` and `section=categories`, and record time to the first visible
chart separately from time to the complete page. Do not claim an end-to-end
speedup from the isolated share-query sample.
