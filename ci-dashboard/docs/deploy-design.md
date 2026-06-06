# CI Dashboard Deployment Design

Status: Draft v0.1

Last updated: 2026-04-17

Reference inputs:
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/ci-dashboard-v1-design.md`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/ci-dashboard-v1-implementation.md`
- `/Users/dillon/workspace/ee-apps-ci-dashboard/ci-dashboard/docs/deploy-design.md`
- `/Users/dillon/workspace/ee-ops/infrastructure/gcp/gateways/gke-gateway.yaml`
- `/Users/dillon/workspace/ee-ops/apps/gcp/prow/release/http-routes.yaml`
- `/Users/dillon/workspace/ee-ops/apps/gcp/jenkins/beta/release.yaml`
- `/Users/dillon/workspace/ee-ops/apps/gcp/jenkins/beta/release/http-routes.yaml`

## 1. Purpose

This document defines how the V1 dashboard application should be deployed to the prow GKE environment and exposed at:

- `https://prow.tidb.net/dashboard/`

This document is intentionally about the dashboard application publish path and runtime packaging.

It does not redesign the V1 data jobs. The jobs remain independently deployable CronJobs and continue to read or write only the project-owned `ci_*` tables plus read-only upstream source tables.

## 2. Target Outcome

The first production-facing deployment should satisfy all of the following:

- reuse the existing `prow.tidb.net` hostname
- publish the dashboard under `/dashboard/` rather than introducing a new host
- run the dashboard as one deployable application:
  - FastAPI backend
  - built React frontend
- keep the browser talking only to the dashboard application HTTP endpoint
- keep upstream source tables read-only
- follow the existing `ee-apps` plus `ee-ops` chart and HelmRelease pattern already used by other internal services

## 3. Existing Production Routing Topology

## 3.1 External Gateway

The GKE cluster already exposes external HTTPS traffic through Gateway API.

Current gateway:

- `infra/external-https`

Current source of truth:

- `/Users/dillon/workspace/ee-ops/infrastructure/gcp/gateways/gke-gateway.yaml`

Important confirmed properties:

- the gateway terminates TLS for `*.tidb.net`
- the gateway accepts `HTTPRoute`
- `allowedRoutes.namespaces.from` is `All`

Implication:

- `ci-dashboard` can be deployed in the same namespace as existing apps or in a new namespace without needing a gateway-side namespace allowlist change

## 3.2 Existing `prow.tidb.net` Path Allocation

Current path routing already in use:

| Host | Path prefix | Backend | Source |
| --- | --- | --- | --- |
| `prow.tidb.net` | `/` | `prow-deck` | `/Users/dillon/workspace/ee-ops/apps/gcp/prow/release/http-routes.yaml` |
| `prow.tidb.net` | `/hook` | `prow-hook` | `/Users/dillon/workspace/ee-ops/apps/gcp/prow/release/http-routes.yaml` |
| `prow.tidb.net` | `/ti-community-owners` | `prow-ti-community-owners` | `/Users/dillon/workspace/ee-ops/apps/gcp/prow/release/http-routes.yaml` |
| `prow.tidb.net` | `/tichi` | `prow-tichi-web` | `/Users/dillon/workspace/ee-ops/apps/gcp/prow/release/http-routes.yaml` |
| `prow.tidb.net` | `/jenkins` | `jenkins` | `/Users/dillon/workspace/ee-ops/apps/gcp/jenkins/beta/release/http-routes.yaml` |

This means `ci-dashboard` should follow the same model:

- same hostname
- distinct path prefix
- one additional `HTTPRoute` rule or one additional `HTTPRoute` resource

## 4. Current `ci-dashboard` Runtime Reality

The current application code already supports the core V1 runtime shape:

- FastAPI serves JSON APIs
- FastAPI exposes `/healthz`, `/livez`, and `/readyz`
- FastAPI serves the built React SPA from `web/dist`
- all non-API paths fall back to `index.html`

Current code facts:

- the backend already acts as the single HTTP entrypoint for API plus SPA
- local frontend development still defaults to root-path deployment, while production build now supports a configurable base path including `/dashboard/`
- the worktree now contains a dedicated `Dockerfile.app` and `Dockerfile.jobs`
- the worktree now contains `charts/ci-dashboard`

## 4.1 What Already Matches the Deployment Goal

Already compatible with the desired single-app deployment:

- one FastAPI process can serve both API routes and SPA assets
- the frontend does not connect to TiDB directly
- the app already has probe endpoints
- the backend path split is already clear:
  - `/api/*` for API
  - non-API routes for SPA

## 4.2 What `/dashboard/` Required

The original frontend implementation was still root-only:

- `BrowserRouter` had no `basename`
- Vite had no configured `base`
- built asset URLs started with `/assets/...`
- API fetches defaulted to `/api/...` at the site root

This deployment work addresses that gap by making the frontend build and router base-path aware for `/dashboard/`.

## 5. Deployment Design Decisions

## 5.1 Host and Path Strategy

V1 should publish the dashboard at:

- `https://prow.tidb.net/dashboard/`

Why this is the preferred first release path:

- it matches the existing team entrypoint already used for prow and GCP Jenkins
- it avoids introducing a new public hostname
- it reuses the existing wildcard TLS and gateway infrastructure
- it keeps operational discovery simple for internal users

## 5.2 Routing Strategy

The dashboard should be exposed through Gateway API `HTTPRoute`, not classic Ingress.

Preferred route rule:

- hostname: `prow.tidb.net`
- path prefix: `/dashboard`
- backend: dashboard Service

Preferred route behavior:

- use `URLRewrite`
- `ReplacePrefixMatch: /`

Example intent:

```yaml
httpRoute:
  enabled: true
  parentRefs:
    - kind: Gateway
      name: external-https
      namespace: infra
  hostnames:
    - prow.tidb.net
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /dashboard
      filters:
        - type: URLRewrite
          urlRewrite:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: /
```

Why rewrite is preferred:

- the backend can continue to serve internal paths at `/`
- FastAPI route definitions do not need to move under `/dashboard`
- SPA fallback can continue to work through the existing catch-all
- the deployment model becomes consistent with existing path-rewritten services in `ee-ops`

## 5.3 Backend Path Strategy

The first deployment should keep the backend internally rooted at `/`.

That means:

- keep API routes defined as `/api/v1/*`
- keep health routes at `/healthz`, `/livez`, `/readyz`
- keep SPA static serving mounted at `/assets`
- keep SPA fallback on non-API paths

Recommended V1 choice:

- do not require FastAPI `root_path` for the initial `/dashboard/` rollout

Reason:

- with `HTTPRoute` prefix rewrite, the backend receives requests without the `/dashboard` prefix
- this keeps backend behavior aligned with current local development and current tests

Fallback option if route behavior changes later:

- add a runtime-configurable `root_path`

But that should be treated as a compatibility escape hatch, not the primary deployment design.

## 5.4 Frontend Base-Path Strategy

Unlike the backend, the browser must know that the public app lives under `/dashboard/`.

Required frontend changes for production:

- Vite build base must become `/dashboard/`
- React router must use `basename="/dashboard"`
- browser API calls must target `/dashboard/api/...` rather than `/api/...`

Recommended V1 behavior:

- keep the backend internal API at `/api/v1/*`
- make the browser call `/dashboard/api/v1/*`
- let `HTTPRoute` rewrite that external path to `/api/v1/*`

This gives a clean split:

- external public path space stays under `/dashboard`
- backend application code can stay internally rooted at `/`

## 5.5 App Packaging Strategy

V1 should add a dedicated application image separate from the jobs image.

Required images:

- `ci-dashboard`
- `ci-dashboard-jobs`

Why the app image must be separate:

- the current `Dockerfile.jobs` is CLI-only
- the dashboard app needs a long-running web server entrypoint
- the app image must include built React assets under `web/dist`
- app rollout cadence differs from job rollout cadence

Recommended app image behavior:

- install the Python package
- copy the built frontend assets
- run `uvicorn ci_dashboard.api.main:app --host 0.0.0.0 --port 8000`

Recommended image repositories:

- `ghcr.io/pingcap-qe/ee-apps/ci-dashboard`
- `ghcr.io/pingcap-qe/ee-apps/ci-dashboard-jobs`

## 5.6 Kubernetes Resource Strategy

The V1 app release should follow the same separation already described in the implementation spec:

- app release resources in one chart
- jobs release resources in a separate chart or separately managed job manifests

For the application itself, `charts/ci-dashboard` should contain:

- `Deployment`
- `Service`
- `ServiceAccount` if needed
- `HTTPRoute`
- config and secret references

Not required for the first release:

- HPA
- separate ingress controller configuration
- sidecar proxy

## 5.7 Namespace Strategy

Recommended initial namespace for the dashboard app:

- `apps`

Reasoning:

- existing ad hoc backfill and sync usage already assumes `apps`
- current database secrets used by local helper scripts are already fetched from `apps`
- first release friction stays lower if we avoid introducing a new namespace before the app is externally reachable

Future option:

- move to a dedicated `ci-dashboard` namespace after the release is stable, if stronger isolation becomes desirable

## 5.8 Secret and Runtime Configuration Strategy

The dashboard app must continue to use runtime-provided database configuration and TLS.

Hard rule for V1 deployment:

- no credential or secret content may be embedded in application source code
- no credential or secret content may be baked into `ci-dashboard` or `ci-dashboard-jobs` images
- no credential or secret content may be committed as literal values in chart defaults or release manifests
- credentials and secrets must be created as Kubernetes Secret objects and referenced by the Deployment or Job manifests at runtime

Required runtime inputs:

- TiDB host
- TiDB port
- TiDB database
- TiDB user
- TiDB password
- CA certificate path or equivalent TLS configuration

Initial rollout recommendation:

- app deployment may reuse the current database secret material already used by manual backfill workflows in `apps`
- the current prow GKE cluster already has an `apps/ci-dashboard-eq-prd-insight-db` secret with the required `TIDB_*` keys
- that secret currently sets `TIDB_SSL_CA` to the system CA bundle path inside the container, so the first app release does not need a separate mounted CA secret

Steady-state recommendation:

- promote those values into app-neutral secret names such as:
  - `ci-dashboard-db`
  - `ci-dashboard-ca`

This keeps deployment naming aligned with a long-running application rather than one-off backfill workflows.

Current implementation note:

- the first `ee-ops` release can point directly at `ci-dashboard-eq-prd-insight-db`
- a separate `ci-dashboard-ca` secret remains optional unless future database connectivity requires a custom CA bundle instead of the system trust store

Practical implication for upcoming implementation work:

- `Dockerfile.app` and `Dockerfile.jobs` contain no secret material
- `charts/ci-dashboard` should expose only secret references, not inline sensitive values
- `ee-ops` `HelmRelease` values should reference existing Kubernetes Secret names rather than storing literal credentials

## 6. Planned Repository and Release Layout

## 6.1 `ee-apps` Changes

Planned application chart:

```text
ee-apps/
  charts/
    ci-dashboard/
      Chart.yaml
      values.yaml
      templates/
        _helpers.tpl
        deployment.yaml
        service.yaml
        httproute.yaml
        serviceaccount.yaml
```

Planned application packaging changes:

```text
ci-dashboard/
  Dockerfile.app
  Dockerfile.jobs
  web/
  src/
```

## 6.2 `ee-ops` Changes

Planned release layout:

```text
ee-ops/
  apps/
    gcp/
      ci-dashboard/
        release.yaml
        release/
          kustomization.yaml
          release.yaml
```

Recommended release model:

- `ee-apps` publishes the `ci-dashboard` chart
- `ee-ops` owns the `HelmRelease`
- the `HelmRelease` enables `httpRoute`
- the `HelmRelease` pins the application image tag

This matches the existing deployment split already used by services such as `cloudevents-server`.

## 7. Required Code Changes Before First Deploy

## 7.1 Frontend

Required:

- add a configurable Vite base path for production builds
- add `BrowserRouter` basename support
- add a configurable public API base prefix

Recommended shape:

- `VITE_BASE_PATH=/dashboard/`
- `VITE_API_BASE_URL=/dashboard`

## 7.2 Backend

Required:

- no route tree redesign is required if prefix rewrite is enabled

Optional hardening:

- add a runtime-configurable `root_path`
- add a startup log line showing the effective public base path

## 7.3 Packaging

Required:

- add `Dockerfile.app`
- ensure the app image build includes a frontend production build step
- ensure the app container starts the FastAPI server instead of the jobs CLI

## 7.4 Chart

Required:

- create `charts/ci-dashboard`
- expose service port `80` to container port `8000`
- enable `HTTPRoute`
- configure probes against FastAPI health endpoints

Recommended probes:

- liveness: `/livez`
- readiness: `/readyz`

## 8. Rollout Sequence

Recommended order:

1. make the frontend subpath-aware
2. add `Dockerfile.app`
3. create `charts/ci-dashboard`
4. create the `ee-ops` `HelmRelease`
5. deploy into GKE with `HTTPRoute` prefix rewrite
6. verify browser load, SPA navigation, API calls, and health endpoints

## 9. Validation Checklist

Before calling the first release done, verify all of the following:

- `https://prow.tidb.net/dashboard/` loads the SPA
- direct SPA deep links such as `https://prow.tidb.net/dashboard/flaky` work
- browser-loaded JS and CSS assets resolve under `/dashboard/assets/...`
- browser API calls resolve under `/dashboard/api/v1/...`
- backend receives rewritten paths and serves the expected API responses
- `/dashboard/healthz` is routable externally if needed for manual checks
- app pods become ready through Kubernetes probes
- no upstream source table is modified by the dashboard app

## 10. Deferred Items

The following are intentionally not required for the first external release:

- dedicated namespace split for the app
- HPA
- auth layer in front of the dashboard
- canary rollout policy
- a separate domain such as `dashboard.tidb.net`
- moving data jobs into the same chart as the app

## 11. Summary of Chosen Direction

The deployment plan for V1 is:

- keep one app that serves FastAPI plus React
- publish it at `https://prow.tidb.net/dashboard/`
- reuse the existing `external-https` Gateway
- expose the app with `HTTPRoute`
- rewrite `/dashboard` to `/` before traffic reaches the backend
- make the frontend explicitly aware of `/dashboard/`
- add a dedicated app image and a dedicated app chart
- keep jobs independently deployable and unchanged in principle
