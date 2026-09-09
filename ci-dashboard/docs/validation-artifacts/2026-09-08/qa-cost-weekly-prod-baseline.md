# QA Cost Weekly production baseline

Captured: 2026-09-08 12:16–12:20 UTC
Endpoint: `GET /api/v1/pages/weekly-cost`
Calendar window returned: `2026-08-31` through `2026-09-06` UTC

## Serving environment

- GKE context: `gke_pingcap-testing-account_us-central1-c_prow`
- Namespace / deployment: `apps/ci-dashboard`
- Live image: `ghcr.io/pingcap-qe/ee-apps/ci-dashboard:v2026.9.6-2-g8ff8111`
- Replicas sampled independently:
  - `ci-dashboard-7d58fb9b8b-2m2v4`
  - `ci-dashboard-7d58fb9b8b-jldnj`

## Method

Each request ran in the live application container against `127.0.0.1:8000`,
using a persistent HTTP connection. This measures the production API and TiDB
read path without OAuth, ingress, or `kubectl port-forward` latency.

- one warm-up request per pod, excluded;
- 20 sequential HTTP 200 samples in total (15 on the first replica, 5 on the
  second); and
- no added concurrency or write operation.

The response contained 10 configured QA sources, 10 history series, and 9,884
bytes. `purpose_schema_available` was `true`.

## Endpoint latency

| statistic | latency |
| --- | ---: |
| min | 4.04 s |
| p50 | 6.39 s |
| p90 | 6.83 s |
| p95 | 7.53 s |
| max | 7.69 s |
| mean | 6.05 s |

Per-replica medians were 6.65 s (15 samples) and 5.75 s (5 samples).

## Database evidence

TiDB statement-summary entries from the same sampling period show the two
queries issued by the endpoint dominate the request time:

| query | digest prefix | observed average | observed maximum | max memory |
| --- | --- | ---: | ---: | ---: |
| eight-week daily history grouped by source/date | `a61181c79b29` | 2.77–3.66 s | 3.75–5.64 s | ~95 MB |
| last-week / previous-week / previous-month source summary | `c9583a0fac95` | 2.05–2.69 s | 2.77–3.16 s | ~72 MB |

`INFORMATION_SCHEMA.STATEMENTS_SUMMARY_HISTORY` aggregates by TiDB instance and
collection window, so those figures are supporting server-side evidence rather
than a one-to-one trace for each HTTP request.

## Comparison rule

Repeat this exact endpoint benchmark immediately after the new version is live,
with the same sample method and no concurrent load test. For an apples-to-apples
calendar window, run it before `2026-09-14T00:00:00Z`; after that time the fixed
weekly endpoint will query a new last-complete-week window. Record response
shape as well as latency: adding project/team share or budget sections will
intentionally add queries and payload.
