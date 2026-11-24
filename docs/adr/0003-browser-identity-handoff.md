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
