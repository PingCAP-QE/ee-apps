# Flaky query workload and failure handling

This change reduces repeated work without changing BR-11/BR-12 attribution or
increasing database retries/timeouts. Standalone endpoints retain their contracts.

## Historical weekly case rates

`case_runs_raw` has two mutually exclusive `UNION ALL` arms:

- A nonempty stored normalized key is used directly.
- NULL/empty stored keys retain the complete legacy URL normalization path.

Both arms retain the original cloud inference and predicates. Historical non-flaky
case/job evidence before the selected start still establishes denominators. Trigger
repo/branch identity, snapshot guards, 24-hour attribution and end-of-window report
lookahead are unchanged. Empty Issue scopes skip the historical query but still
return the requested week columns, metadata and unknown-denominator summaries.

The preceding read-only investigation found the split faster on two sampled scopes
with equal complete results (roughly 5s to 1.5–2.2s for the open one-week SQL;
roughly 8s to 2–2.24s per query for two concurrent queries). These are diagnostic
measurements, **not a guaranteed production latency or an eliminated full scan**:
the split can scan the source twice. No production load test, DDL, index hint or
configuration change is part of this implementation.

## Request-local sharing

The page groups related projections into two tasks:

- Issue lifecycle/fix progress: materialize latest Issue rows once and build three
  views. Linked PR rows are reused only when current and previous Issue key lists
  match; PR states are still evaluated separately at their respective dates.
- Build charts: fetch raw buckets once per needed granularity, then apply each
  view's existing complete/partial-week policy separately. Week needs one raw
  query; Day/Month need two.

Only materialized row dictionaries are reused, within a single request. There is
no global cache and no SQLAlchemy Connection is shared across executor threads.
The original response keys and metadata remain intact.

A representative one-week page with a nonempty Issue scope and identical
current/previous Issue keys uses **11 business SELECTs instead of 16**: latest
Issues 3→1, raw week buckets 3→1, and linked PRs 2→1. Day/Month use 12 SELECTs
under those conditions. Counts vary for empty/new Issue scopes or missing dates;
these are not universal per-page limits.

Top cases now sends only effective repo/branch/job/cloud/date filters. Changing
Issue status or chart granularity neither refetches nor aborts its independent
request. Bucket selections are scoped to the page filters before effects run, so
old bucket dates cannot initiate a request against newly selected filters. Late
responses from aborted fetches cannot overwrite newer results.

## Failure boundary

Non-SQLite page sections use a sliding window of at most four submitted tasks.
Completed batches are checked for exceptions before submitting replacements. On
failure, further submissions stop and pending futures are cancelled. Successful
results retain task insertion order; failures retain the original exception.

Cancellation **does not stop already-running SQL**. Executor shutdown still waits
for running tasks to finish connection cleanup. SQLite remains serial. This is a
per-request bound, not global admission control: multiple requests, independent
Top/filter endpoints and users can still overlap. HTTP abort/disconnect is not
propagated as server-side SQL cancellation.

The investigated gateway's default route budget (15s) versus SQL/socket budgets
(30s/35s) remains a separate deadline-design issue. Independent lightweight-query,
TLS/greeting and network/provider failures are not repaired by query sharing.
This change therefore does not claim to eliminate every HTTP 500/504.

## Regression coverage

`tests/api/test_flaky_workload.py` checks empty scopes, stored/NULL/empty keys against
an unsplit SQL oracle, historical non-flaky membership, grouped versus independent
page output and query counts, partial weeks, request isolation, and equal/different
linked keys. Existing API tests cover cloud, URL, GitHub Actions, snapshot,
cross-repo/branch, 24-hour, lookahead and unknown-denominator contracts.

The executor tests use a real ThreadPoolExecutor with Barrier/Event synchronization:
a later failure must be observed while earlier tasks are still running, no fifth
task may be submitted in that scenario, and returning waits for cleanup. Success
checks the four-worker bound and response order. No timing sleeps are used.

`web/src/__tests__/FlakyRequests.test.js` checks effective/ignored filters, pending
Top requests, scope changes and aborted late responses. The normal commands are
`pytest tests/api`, `npm test` and `npm run build` (run each in its subproject).
