# EE Portal

EE Portal is a configuration-driven engineering workbench served at `/ee`. It intentionally supports four product surfaces—catalog, list, form, and detail—rather than arbitrary low-code pages.

## Development

Node.js 24 is required.

```sh
npm ci
npm run config:compile
npm run dev
```

The local UI is available at `http://127.0.0.1:5173/ee/`. Set `PORTAL_API_PROXY` to a TiBuild server URL when exercising real API calls.

## Configuration contract

Each module contains a `module.yaml` and `pages/*.yaml` documents using `apiVersion: ee.pingcap.net/v1alpha1`. The compiler produces a deterministic `registry.json` with compiler/runtime versions and a SHA-256 source digest.

The compiler permits only same-origin named data sources, declarative request values from form/route/query/state/response data or constants, JSON Pointer response mappings, registered formatters, standard actions, and allowlisted plugin IDs. It rejects absolute URLs, custom headers, secrets, scripts, duplicate routes, stale references, unsupported actions/plugins, and incompatible JSON Schema versions.

Form pages use JSON Schema draft 2020-12 and RJSF's eval-free Cloudflare Worker validator. The production CSP does not allow `unsafe-eval`.

## Verification

```sh
npm run config:check
npm test
npm run build
npm run test:e2e
helm lint ../../charts/ee-portal
```

The Playwright suite covers desktop and mobile catalog/list/create/detail/rerun flows with mocked same-origin APIs. Production module sources and the generated registry live in `PingCAP-QE/ee-ops/apps/gcp/ee-portal` so configuration changes roll independently of this image.
