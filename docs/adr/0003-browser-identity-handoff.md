# ADR 0003: Browser identity handoff to authzd

- Status: Accepted — Option D (native `EnvoyProxy.spec.filterOrder`), subject to
  the two validation phases below. Browser routes stay out of `extAuth` until both
  pass.
- Date: 2026-11-24, evidence added 2026-11-25, decision 2026-11-25

## Context

Browser routes use Gateway OIDC cookies and CSRF protection. The ext_authz
service must receive a verifiable user identity without trusting client-provided
headers. Gateway v1.9.3 behavior and filter ordering must be proven on the
deployed CRDs and data plane.

## Recommendation

Forward a signed Google ID token using an Envoy Gateway-supported, explicitly
configured header only after a rendered configuration and live request prove
that the gateway overwrites/removes any client value. `authzd` independently
validates issuer, audience, signature, expiry and subject. Never accept
`X-User-*`, tenant, email, or identity headers as authority. Keep browser and
machine JWT validation paths distinct.

## Consequences

No browser route should attach extAuth until the exact header, filter ordering,
cookie behavior, callback behavior, and spoofing test are recorded. The proposal
does not claim that `forwardIDToken` is currently enabled or correct.

## Approval

Pending: Gateway owner and security owner.

## GCP deployment evidence (2026-11-25)

The deployed Gateway controller is v1.9.3. Its installed `SecurityPolicy` CRD
exports `extAuth.contextExtensions`, `failOpen`, `timeout`, `statusOnError` and a
gRPC `backendRefs` list, and the OIDC policy exports a raw ID token for a custom
forwarding header.

### Measured filter order

Taken from the live Envoy admin `config_dump` after `extAuth` was attached to
`tibuild-v2-machine-jwt` (the policy reports `Accepted=True`):

```text
1. csrf
2. ext_authz   (per-route enabled; disabled at chain level)
3. oauth2
4. jwt_authn
5. rbac
6. local_ratelimit
7. credential_injector   (github-token-broker upstream credential)
8. router
```

`ext_authz` runs **before** both authentication filters and before the token
broker's credential replacement. Per-route `typed_per_filter_config` confirms the
filter is enabled on the machine and privileged routes and absent from the
browser GET/HEAD/OPTIONS rules.

### What this falsifies

`forwardIDToken` is attached by the OIDC and JWT filters, which run *after*
`ext_authz`. A browser request therefore reaches `authzd` without the forwarded ID
token, so the recommended mechanism cannot deliver a browser identity. This is the
one part of the previous recommendation that the evidence rules out; the rest of
the contract (independent verification, no identity headers, separate modes)
still holds and is already implemented.

### What the order confirms

- Requests that OIDC/JWT will later reject do reach `authzd`. In observe mode they
  are recorded as `identity_invalid`, which is useful signal, not a regression:
  the later filters still reject them.
- The token broker contract from the design holds: `credential_injector` runs
  after `ext_authz`, so `authzd` sees the caller's original `Authorization`
  header and does not need to reconstruct the external identity from an internal
  credential.

## Options for browser identity

| Option | Mechanism | Cost / risk |
| --- | --- | --- |
| A. Keep browser routes out of `extAuth` | Browser keeps OIDC + CSRF and the application's own checks; no browser `extAuth` in V1 | No resource-level gateway decision for browser reads. Acceptable while every `/tibuild` mutation is machine-only and ADR 0002 keeps reads open to authenticated staff |
| B. `authzd` validates the OIDC ID-token cookie | `authzd` reads the configured `idToken` cookie and verifies it against Google JWKS and the OAuth client audience | Needs a cookie-aware verifier and an explicit allow-list of unauthenticated browser paths, or an expired cookie is denied before `oauth2` can redirect to login. Also needs a spoofing test: a client-supplied cookie is untrusted input, verified the same way as a bearer token |
| C. Reorder filters with `EnvoyPatchPolicy` | Patch the HCM `http_filters` order so `ext_authz` follows `oauth2`/`jwt_authn` | Unsupported surface, applies to the whole listener, and reverses the broker advantage above. Not recommended |

### Recommendation

Take option A for V1 and record it as an explicit exception: the browser route
keeps OIDC, CSRF and the service-side checks, and receives no `extAuth` fragment.
Revisit only if browser reads need row-level scoping or a browser mutation becomes
externally reachable — at that point option B is the viable path, with the
unauthenticated-path allow-list and the cookie spoofing test designed up front.

Option A also keeps the design statement honest: the gateway does not authorize
browser reads, and nothing in this ADR claims it does.

## Decision (2026-11-25)

Gateway architect, security owner: proceed with **option D**. `filterOrder` is a
first-class Envoy Gateway v1.9 feature, so the fix is configuration, not a custom
xDS patch and not cookie verification inside `authzd`. Option A remains the
rollout boundary until validation passes; no business service changes.

### Validation phases

**Phase A — isolated Gateway PoC** (`PingCAP-QE/ee-ops`
`infrastructure/gcp/poc-authz-filter-order/`): prove the resulting chain, that
`jwt_authn` rejects before `ext_authz` (and therefore records no decision), that an
authenticated request reaches `authzd`, that the caller's original `Authorization`
header is still present, and that `credential_injector` still runs afterwards.

**Phase B — real OIDC integration** on a host that can receive the OAuth callback:
prove `forwardIDToken` delivery, browser session binding, login/callback/refresh/
logout, header sanitization, CSRF, and a browser ReBAC allow/deny decision. Use a
dedicated OAuth client and test redirect URI if that avoids touching the shared
production Gateway; otherwise a controlled canary in observe mode. Browser
enforcement stays off until Phase B passes.

### Contracts this ADR now fixes

1. **Filter order is a contract.** `ext_authz` must run after the authentication
   filters that produce identity and before `credential_injector`. It is versioned
   in the `EnvoyProxy`, verified by reading the admin `config_dump`, and recorded in
   every shadow report. `filterOrder` is proxy-wide, so any change is evaluated
   route by route.
2. **Identity substitution is the acceptance test**, not the absence of malformed
   tokens: establish a browser session for user A, present a valid ID token for
   user B in the forwarding header, and prove `authzd` decides on user A. A
   deployment that accepts B fails the contract even if it rejects malformed or
   duplicated values.
3. **One credential per header.** `authzd` rejects a forwarded identity header
   that carries a comma, embedded whitespace or an empty value, with the same
   strictness as `Authorization`. This is a prerequisite for browser integration
   and is already implemented; it bounds the damage if a Gateway ever fails to
   overwrite a client-supplied copy.
4. **Three failure classes stay distinguishable.** A Gateway authentication
   failure produces no `authzd` decision; a handoff failure produces
   `identity_missing` or `identity_invalid`; an authorization denial produces
   `relationship_denied`, `resource_not_found` or `route_not_registered`.
   Reordering resets the shadow baseline: pre- and post-reordering counts are not
   comparable, and `identity_invalid` becomes a signal of handoff problems rather
   than of ordinary unauthenticated traffic.

Upstream note: Envoy v1.39 release notes state that OAuth2 `forward_id_token`
strips client-supplied custom forwarding headers and sets them only from a
validated cookie. Treat that as supporting evidence, not proof — it is verified
against the deployed image in Phase B.

## Phase B mode-1 results (2026-11-26)

Run on an isolated Gateway (`EnvoyProxy.spec.filterOrder`, `ext_authz` after
`oauth2`/`jwt_authn`), a dedicated OAuth client, and a `/authz-canary` route whose
registry rule uses `capability:authz.canary` so every decision is attributable.

Confirmed:

1. **`forwardIDToken` reaches `ext_authz`.** A browser session produced a
   decision with `principal=user:google/sub/<sub>` — a subject `authzd` derived
   from the forwarded ID token by verifying it against Google JWKS with the
   dedicated client as audience. This is the mechanism the previous revision
   called unproven.
2. **The decision uses the session identity, not the client's claim.** Requests
   carrying a client-supplied `X-Authzd-ID-Token` (a garbage value, and a
   duplicate `a, b` value) were both decided on the session user. The Gateway
   overwrote the header in both cases; the duplicate form would also have been
   rejected by `authzd`'s single-credential rule.
3. **Gateway-rejected requests still produce no decision.** An unauthenticated
   request is answered by the OIDC filter with a redirect and leaves no decision
   record, which is the behaviour the reordering introduced.
4. **The canonical subject encoding had to change.** The subject inherited from
   ADR 0001 (`user:<issuer-url>|<sub>`) is rejected by OpenFGA because the
   identifier contains `:`. See the constraint recorded in ADR 0001; the deployed
   prefix is now `user:google/sub/`.

Residual gap, explicitly not claimed: the substitution test with a **valid ID
token belonging to another user** has not been executed, because no second
identity was available. Envoy's overwrite does not inspect the value, and both of
the above probes show replacement, but the ADR does not treat that as proof.
Options to close it, in order of cost: a dedicated Workspace test account that can
complete one login; a colleague's token; a temporary `serviceAccountTokenCreator`
grant so a service-account token with this audience can be minted.

Until that runs, the browser route stays out of `extAuth`.
