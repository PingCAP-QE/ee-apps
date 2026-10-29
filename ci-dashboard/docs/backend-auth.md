# Dashboard backend identity boundary

## Policy and scope

Every company Workspace user may read Dashboard data. No team-level RBAC or new
login flow is required. Google Workspace Internal OIDC at Envoy remains the
browser login mechanism; the application independently verifies Google's signed
ID token so that a direct Service or Pod request cannot skip identity checks.

The 2026-10-29 read-only investigation found anonymous `200 application/json`
responses from `/api/v1/pages/cost-sources` over both Service and Pod IPs, including
from `jenkins/jenkins-0`. Public `/dashboard/api/...` requests instead redirected
to Google, and an external account was rejected with `403 org_internal`.
These observations demonstrate an internal bypass, not anonymous public access.
The GKE cluster's NetworkPolicy add-on was disabled, so a policy manifest alone
would not establish this boundary.

This change protects HTTP APIs, frontend/static files, and OpenAPI/docs before
routing, validation or SQL. Only exact GET/HEAD `/healthz`, `/livez` and `/readyz`
paths are exempt. Readiness still checks authentication configuration and DB
connectivity; liveness does not depend on either. There are no WebSocket routes.

## Credential contract

Envoy forwards the **decrypted Google ID token**, not the opaque access token or
an email assertion, in exactly one `X-CI-Dashboard-ID-Token` request header.
The backend verifies:

- RS256 signature against public certificates from the fixed Google OAuth certs
  endpoint, with a bounded token size;
- Google issuer, the configured OAuth client audience, issued-at and expiration
  (with a 30-second clock-skew allowance);
- nonempty subject/email, and matching authorized party when `azp` is present;
- strict boolean `email_verified: true` and a signed `hd` Workspace domain from
  the configured exact allowlist. An email suffix alone does not grant access.

The Google verification library performs signature/time/audience verification.
Fetched PEM key material is parse-validated before caching; malformed provider
key material returns 503 with the retry cooldown, rather than caching a broken
key and misclassifying valid users as 401. Tests exercise Google's X.509 PEM
certificate format.
No client secret or signing private key is copied into the application. The
application ignores `Authorization`, cookies and self-reported identity headers.
A direct caller with a valid, intended-audience company ID token is authorized:
identity, not the network path, is the trust boundary. Token holders must keep
these bearer credentials confidential; the gate does not provide immediate
revocation before token expiry or replace database/workload isolation.

Certificate downloads have a five-second HTTP timeout, run off the ASGI event
loop, and are shared under a lock. Known, unexpired cached keys remain usable
without waiting for an unknown-key refresh. Other followers wait at most 100 ms
for an in-flight fetch, then return 503 rather than occupy worker threads for
the full download.
Certificate cache lifetime follows the advertised `max-age`, floored at 60 seconds
and capped at one hour; without `max-age`, it is one hour. Unknown key IDs may
force a refresh, but a ten-second cooldown after every successful or failed fetch
prevents attacker-controlled key IDs from causing repeated downloads. A newly
rotated key may return 401 during that cooldown, then succeeds after a refresh
retrieves it. Refresh failures return 503; valid unexpired cached keys remain
usable, but expired keys are never used during outages. Protected API responses
are `private, no-store`.
Credentials, claims and verifier exception details are not returned or logged.
Missing/invalid configuration logs only the affected environment variable name,
once per variable per process; values are never included.

## Required deployment configuration

**Application merge alone is not a production remediation.** Coordinate the
following `ee-ops` configuration with the application image rollout.

The deployed Envoy Gateway v1.9.0-rc.1 CRD supports `oidc.forwardIDToken`. Keep
OIDC, token encryption, CSRF, and refresh enabled. Retain the `openid`, `email`
and `profile` scopes: `email` is required for the email/verified-email claims.
Google must also emit a signed Workspace `hd` claim matching an approved domain;
requesting scopes or having a company-looking email address does not replace it.
Extend the existing
`apps/ci-dashboard-oidc` SecurityPolicy without adding auth pass-through:

```yaml
spec:
  oidc:
    # Keep the existing issuer, client references, scopes and redirectURL.
    forwardIDToken:
      header: X-CI-Dashboard-ID-Token
```

Do not forward an access token in place of the ID token, disable cookie token
encryption, or treat a forwarded email as a signed credential. Envoy owns the
forwarded token header; the backend nevertheless independently verifies it.
The actual deployed CRD and Envoy configuration must support this field, rather
than relying only on a manifest rendering successfully. The verified data-plane
version was Envoy v1.39.0. Its [OAuth2 filter source](https://github.com/envoyproxy/envoy/blob/v1.39.0/source/extensions/filters/http/oauth2/filter.cc#L885-L919)
removes a client-supplied forwarding header before authentication, then
[decrypts and forwards the ID token](https://github.com/envoyproxy/envoy/blob/v1.39.0/source/extensions/filters/http/oauth2/filter.cc#L1125-L1189).
[Refresh forwards the replacement token](https://github.com/envoyproxy/envoy/blob/v1.39.0/source/extensions/filters/http/oauth2/filter.cc#L1569-L1638)
only when the provider supplies a new `id_token`; this still needs real Google
session-refresh validation, not just source inspection.

Add these environment entries to the Dashboard **app** HelmRelease values:

```yaml
- name: CI_DASHBOARD_OIDC_CLIENT_ID
  valueFrom:
    secretKeyRef:
      name: ci-dashboard-oidc
      key: client-id
- name: CI_DASHBOARD_OIDC_HOSTED_DOMAINS
  value: pingcap.com
```

Confirm the approved Workspace `hd` domain(s) before rollout. Multiple approved
domains may be supplied as a comma-separated exact list, not wildcard/suffix
patterns. The audience must match the client used by the Dashboard OIDC policy.
Do not set `CI_DASHBOARD_AUTH_DISABLED` in production.

Authentication is enforced by default. Missing/invalid configuration produces
503 for protected paths and readiness, never anonymous access. Missing, invalid,
expired or forged tokens produce 401; a verified noncompany/unverified-email
identity produces 403. Certificate unavailability produces 503, not fail-open.

### Rollout and validation

1. Prepare/review the gateway forwarding and app environment changes in
   `ee-ops/apps/gcp/ci-dashboard/{security-policy,release}.yaml`. They can precede
   the new image: the old application ignores the extra environment/header,
   but remains vulnerable until the new app is deployed. Confirm the live
   SecurityPolicy is Accepted before proceeding.
2. After app merge, derive the exact image tag from the successful release
   workflow and verify the published image; do not guess a future tag. Before
   replacing the working app, verify HTTPS egress from a Dashboard pod to the
   fixed Google certs endpoint. Use an authorized private canary of that new
   image, with the same app environment and Envoy OIDC forwarding, to exercise
   a company user's authenticated API request. Require 200 for that request and
   401 without credentials; this checks the actual forwarded token's audience,
   authorized party, Workspace domain and verified-email conditions. Record only
   pass/fail results: do not dump tokens or claims into logs, files or chat.
   Readiness alone is not sufficient: it checks configuration presence/shape,
   not a real token or ongoing certificate reachability. Halt the main rollout
   if the canary or cert egress fails. Canary/route changes require rollout
   authorization and are not performed by this code change.
3. Once the canary passes, roll out that same verified image with the required
   environment entries. No cluster CNI or database change
   is needed. A failed readiness check should stop the rolling replacement;
   do not make it pass by enabling the development bypass.
4. Confirm `/livez` and `/readyz`, company browser login and Cost/CI reads. Test
   session refresh as well as initial login. The browser needs no new header or
   frontend token storage: Envoy forwards the token after OIDC authentication.
5. Repeat unauthenticated Service and Pod requests from the worker and a
   separate namespace; sensitive paths must return 401, with no SQL/data access.
   Verify fabricated email headers and fake token signatures do not change this.
6. Confirm external accounts remain rejected, public anonymous API requests
   still redirect to Google, and cost/owner responses do not enter shared caches.
7. Only after these checks, submit the deployment and test evidence to security
   for verification. Do not claim a production fix or clean rescan from unit
   tests or PR merge alone.

Rollback to the previous image reopens the internal bypass and needs explicit
approval. Gateway forwarding/environment can remain in place during an approved
rollback. Never use `CI_DASHBOARD_AUTH_DISABLED=true` as a production login fix.

## Local development and regression tests

`make api` and `make api-tidb` explicitly set `CI_DASHBOARD_AUTH_DISABLED=true`
for the loopback-only development server. For a manually launched local server,
set this variable explicitly; do not use it with externally reachable listeners
or untrusted real data. Other values such as `yes` fail closed.

Existing query tests explicitly use local-development mode. The dedicated auth
suite removes that bypass and uses generated RSA keys plus mocked public
certificate HTTP responses; no live credentials, provider calls or real cost
records are needed. It covers actual app routes, signed claim checks, signature
forgery, duplicate headers, clock skew, certificate rotation/caching/failure/concurrency,
fail-closed configuration, probes, and the authenticated cost-sources route.

```bash
cd ci-dashboard
python -m pytest tests/api/test_auth.py tests/test_api_main.py
COVERAGE_FILE=/tmp/ci-dashboard.coverage python -m pytest \
  --cov=src/ci_dashboard --cov-report=term-missing
```

These are backend boundary tests, not a claim that real Google login, Envoy
forwarding, token refresh, or production deployment have been tested after this
change. Those checks belong to the authorized coordinated rollout above.
