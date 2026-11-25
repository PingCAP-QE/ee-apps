# ADR 0003: Browser identity handoff to authzd

- Status: Proposed; blocked on a decision — the recommended header forwarding is
  not reachable in the deployed filter order (evidence below)
- Date: 2026-11-24, evidence added 2026-11-25

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

## Decision needed

Gateway owner and security owner: confirm option A as the V1 boundary, or choose
option B and accept the cookie verifier, the unauthenticated-path allow-list and
the redirect behavior in enforce mode.
