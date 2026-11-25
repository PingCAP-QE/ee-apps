# ADR 0003: Browser identity handoff to authzd

- Status: Proposed; pending Envoy/Gateway evidence and security approval
- Date: 2026-11-24

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

The deployed Gateway controller is v1.9.3. Its installed SecurityPolicy CRD
describes a raw ID token for a custom forwarding header and a Bearer token for
Authorization. The live tibuild-v2-oidc policy omits forwardIDToken and extAuth.
The active eg-internal Envoy listener orders CSRF, OAuth2, JWT authentication
and RBAC before the router; no ext_authz filter was present. These observations
establish the baseline. Filter ordering and header replacement with extAuth
attached still require a routed test before browser mode can be used.

For browser mode, configure a dedicated forwarded-token header, the OAuth
client audience, and a user principal based on the signed sub claim. For machine
mode, retain the machine JWT audience, workload principal and existing Gateway
allowlist. The service selects one verifier from trusted route context. A token
that fails its selected verifier must end that decision. The service rejects a
shared header or a shared issuer/audience pair across these two modes at startup.
