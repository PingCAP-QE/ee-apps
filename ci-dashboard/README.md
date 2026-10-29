# CI Dashboard

This directory contains the first implementation slice for the CI dashboard V1 project.

Current scope in this scaffold:

- SQL migrations for the three V1-owned tables
- shared Python config and database helpers
- job state persistence helpers
- build URL normalization and exact retest parsing helpers
- implemented data jobs:
- `ci-sync-builds`
- `ci-sync-pr-events`
- `ci-sync-flaky-issues`

`sync-flaky-issues` may call the GitHub API to recover issue/PR metadata when the read-only `github_tickets` source omits it. In Kubernetes, the job authenticates to the in-cluster `github-token-broker` with its ServiceAccount (`serviceAccountName: ci-dashboard`) and requests repository-scoped App installation tokens. A static `GITHUB_TOKEN` is used only when `CI_DASHBOARD_GITHUB_PAT_FALLBACK=true` is set explicitly; broker failures otherwise fail the job instead of silently degrading to a PAT.
  - `ci-refresh-build-derived`
  - `backfill-range` for idempotent date-window re-imports
  - one-off Kubernetes backfill helpers under `k8s/backfill/`
  - recurring flaky issue sync helper under `k8s/cronjobs/`
- implemented read-only FastAPI query endpoints for:
  - `/api/v1/status/freshness`
  - `/api/v1/filters/*`
  - `/api/v1/builds/*`
  - `/api/v1/flaky/trend`
  - `/api/v1/flaky/composition`
  - `/api/v1/flaky/distinct-case-counts`
  - `/api/v1/flaky/issue-weekly-rates`
  - `/api/v1/flaky/top-jobs`
  - `/api/v1/flaky/period-comparison`
  - `/api/v1/failures/*`
- first React dashboard shell under `web/`, including:
  - `Overview`
  - `Build Trend`
  - `Flaky`
- project docs under `docs/`, including `functional-design/`

Not implemented yet:

- deeper build/failure drill-down pages and richer table views
- infra / Jenkins specific pages

## Local Test Run

This scaffold supports local unit tests with sqlite and does not require a remote TiDB instance.

Recommended local setup:

```bash
cd ci-dashboard
python3 -m venv .venv
./.venv/bin/pip install fastapi uvicorn SQLAlchemy PyMySQL pytest pytest-cov ruff coverage httpx
PYTHONPATH=src ./.venv/bin/python -m pytest --cov=src/ci_dashboard --cov-report=term-missing
make sync-builds
make sync-pr-events
make sync-flaky-issues
make refresh-build-derived
./.venv/bin/python -m ci_dashboard.jobs.cli backfill-range --start-date 2025-12-01 --end-date 2025-12-07
make api
make web-install
make web-dev
make web-build
```

Notes:

- tests use sqlite fixtures and run locally
- production-style DB config still uses `TIDB_*`
- database limits default to a `10 + 10` connection pool, a 5-second pool/connect wait, a 30-second query/write deadline, and a 35-second read deadline; the query timeout must not exceed the read timeout; override them with the `CI_DASHBOARD_DB_POOL_SIZE`, `CI_DASHBOARD_DB_MAX_OVERFLOW`, `CI_DASHBOARD_DB_POOL_TIMEOUT_SECONDS`, `CI_DASHBOARD_DB_CONNECT_TIMEOUT_SECONDS`, `CI_DASHBOARD_DB_READ_TIMEOUT_SECONDS`, `CI_DASHBOARD_DB_WRITE_TIMEOUT_SECONDS`, and `CI_DASHBOARD_DB_QUERY_TIMEOUT_SECONDS` environment variables
- local development can use `CI_DASHBOARD_DB_URL=sqlite+pysqlite:///./ci-dashboard.sqlite`; MySQL/TiDB pool and timeout controls are ignored for SQLite URLs
- FastAPI serves the built frontend from `web/dist` after `make web-build`
- set `CI_DASHBOARD_STATIC_DIR` when the built frontend lives outside the default repo or container layout
- the Cost and CI details insight tabs are always available
- the Cost tab prioritizes its top spend/breakdown requests: share and trend
  charts have independent loading/error states, and Engineering Group shares
  load after those requests settle for the selected filter scope. Changing only
  the breakdown grouping or drilldown does not refetch Engineering Group data.
  Resource details remain on demand, including while other charts are loading.
  Summary cards show placeholders until spend is available instead of displaying
  pending or failed queries as zero spending.
- the Cost tab uses T+3 data availability in UTC. End has a hover/focus info hint,
  its calendar and URL filters cap End at the latest available date, and the
  default End date uses the same three-day lag.
- the Cost tab supports Day, Week, and Month buckets. Day is limited to 14 days
  including both dates. Week requires the end date to be strictly before the
  start date plus two calendar months (clamped to the target month's last day
  when necessary): July 1 through August 31 allows Week, but July 1 through
  September 1 requires Month. The UI disables incompatible options and
  automatically coarsens invalid Day/Week selections; the Cost API enforces these limits.
  Month requests require the end date to be strictly before the start date plus
  12 calendar months, using the same month-end clamping rule. For example,
  July 1, 2025 through June 30, 2026 is allowed; July 1, 2026 is not.
  Cost API requests without dates default to the last eight complete weeks for
  Week or the last 12 complete calendar months for Month. Day requires both
  dates explicitly. Supplying only one date, a reversed range, or an oversized
  range returns HTTP 422 before any cost query runs. The monthly limit is
  enforced server-side even when the UI or a direct caller requests a longer range.
- during UI iteration, run `make api` and `make web-dev` in separate terminals
- `backfill-range` is stateless and does not update `ci_job_state`, so the same time window can be re-imported safely
- `CI_DASHBOARD_REFRESH_BUILD_LIMIT` controls how many impacted builds one `refresh-build-derived` run will process before checkpointing and continuing in the next CronJob run; the default is `5000`, valid values are positive integers, and smaller values trade shorter/faster runs for more CronJob passes before backlog catch-up finishes
- `CI_DASHBOARD_FORCE_FLAKY_STALE_CLEANUP=true` lets `sync-flaky-issues` apply stale flaky issue cleanup after an intentional large `flaky-test` label purge; empty source sets still skip cleanup, and the one-off Job renderer exposes this as `--force-flaky-stale-cleanup true`
- `CI_DASHBOARD_GITHUB_BROKER_URL` selects the GitHub token broker used by `sync-flaky-issues` (default `http://github-token-broker.github-token-broker.svc`; an empty value disables the broker). `CI_DASHBOARD_GITHUB_BROKER_TOKEN_FILE` overrides the projected ServiceAccount token path, and `CI_DASHBOARD_GITHUB_PAT_FALLBACK=true` opts into a logged `GITHUB_TOKEN` fallback.
- `scripts/render_backfill_job.sh` renders a one-off Kubernetes Job manifest for GKE backfill runs
- `scripts/render_flaky_issue_sync_cronjob.sh` renders a recurring Kubernetes CronJob manifest for daily flaky issue sync
- `scripts/render_jenkins_worker_deployment.sh` renders the V3 Jenkins event worker Deployment manifest
- `scripts/render_archive_error_logs_cronjob.sh` renders the V3 Jenkins error-log archive CronJob manifest
- When a Jenkins finished event's `PROW_JOB_ID` matches an existing build, its
  canonical build URL must also agree. Relative URLs and
  `jenkins.jenkins.svc.cluster.local` URLs are controller-ambiguous: their full
  canonical path must match, then the worker keeps the Prow-matched row's
  controller for the build, audit URL, cloud phase, and timings request. This
  includes Tencent (`do.pingcap.net`); the topic is not assumed to be GCP-only.
  Explicit public controller URLs must agree with the stored controller too.
  Normalization accepts trailing-slash/redirect variants while keeping different
  jobs, build numbers, and staging paths distinct. A conflicting, missing, or
  invalid stored URL fails closed: no build row is updated or inserted via a
  fallback, and no timings enrichment is queued.
  The failed event remains in the audit table; Kafka offsets follow the existing
  audited-failure commit policy, so these events are not retried automatically.
  Repair the stored identity using the trusted Prow sync, then explicitly replay
  the failed event with the same event ID; replay stays failed until the conflict
  is resolved. Without a Prow-ID match, existing new-build and URL-only merge
  behavior (including the GCP default for ambiguous URLs) is unchanged.
- Jenkins updates also check the matched URL and compatible Prow ID in the write
  predicate so a stale lookup cannot overwrite a changed merge target. SQLAlchemy
  enables MySQL/PyMySQL matched-row counting, so identical deliveries still match
  rather than being mistaken for identity changes. Conflict audit reasons include
  the row/Prow IDs and canonical event/stored URLs, not raw URL credentials. The
  authoritative `sync-builds` reconciliation rules, new-build ingestion, and
  URL-based merging of events without a Prow ID are unchanged. These checks
  are defense in depth, not producer authentication: `/events` authentication,
  producer authorization, and Kafka producer ACLs still require separate review.
- Jenkins finished events asynchronously enrich the canonical build row from the
  Jenkins `/timings/` page. This fetch is best-effort and does not block Kafka
  offset commits.
- `backfill-jenkins-timings --lookback-days 30` provides a one-off historical
  repair command; it is intentionally not deployed as a recurring CronJob.
- `CI_DASHBOARD_JENKINS_INTERNAL_BASE_URL` overrides only `prow.tidb.net` (GCP)
  Jenkins requests. `do.pingcap.net` (Tencent) and other controllers keep their
  original host for console logs, pipeline-node APIs, and timings.
- Error Catalog excludes effective `OTHERS / SUPERSEDED_BY_NEWER_BUILD`
  cancellations, including from its trends, rankings, drilldowns, and coverage.
  Raw build states and taxonomy records remain unchanged. Historical Tencent
  archive recovery is described in [the repair runbook](docs/tencent-jenkins-error-archive-repair.md).

## Local Frontend Against TiDB

For local UI iteration with real TiDB data:

```bash
cd ci-dashboard
make prepare-local-tidb-env
make PYTHON=./.venv/bin/python api-tidb
```

In a second terminal:

```bash
cd ci-dashboard
make web-install
make web-dev
```

Notes:

- `make prepare-local-tidb-env` reads the current Kubernetes secrets:
  - `apps/ci-dashboard-eq-prd-insight-db`
  - `apps/ci-dashboard-backfill-ca` as CA fallback when the DB secret exposes a container-only `TIDB_SSL_CA` path
- it writes local-only files under `ci-dashboard/.local/`
- Vite proxies `/api/*` to `http://127.0.0.1:8000`, so the browser still talks only to the local dev server
- you can verify the chain with:
  - `curl http://127.0.0.1:8000/api/v1/status/freshness`
  - `curl http://127.0.0.1:5173/api/v1/status/freshness`

## Daily cost source freshness

The existing `check-data-freshness` job can also compare each active cost account's
BigQuery availability against imported summary and attribution dates. It is opt-in:
set `FRESHNESS_COST_BQ_ENABLED=true`, an explicit `FRESHNESS_COST_BQ_PROJECT`, and
`FRESHNESS_COST_BQ_LOCATION=us-west1`. Use the existing dedicated warehouse Workload
Identity, not shared-account impersonation. Enabled runs include signed date diffs
in the daily Lark report; `FRESHNESS_DRY_RUN=true` reads and prints without sending.

See [the design and rollout contract](docs/cost-source-freshness-design.md) for
bounded query budgets, importer eligibility, explicit dormant-source exceptions,
and AWS split/F04 scope limitations. The checker does not import or repair data.

## Container Builds

This project now provides two container entrypoints:

- `Dockerfile.app` for the FastAPI + React dashboard app
- `Dockerfile.jobs` for CLI-driven sync and backfill jobs

Recommended image repositories:

- `ghcr.io/pingcap-qe/ee-apps/ci-dashboard`
- `ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs`

Repository-local image build wiring is defined in `skaffold.yaml`:

```bash
cd ci-dashboard
skaffold build --push=false --default-repo ghcr.io/pingcap-qe/ee-apps
```

Notes:

- `Dockerfile.app` builds the frontend with production base path `/dashboard/`
- `Dockerfile.jobs` now installs directly from source and no longer depends on a prebuilt wheel in `dist/`
- local-only files under `.local/` are excluded from the Docker build context and are not baked into images
