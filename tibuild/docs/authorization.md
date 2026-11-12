# Authorization

Authentication is the Gateway's job. This service never validates an OIDC
session or an ID token; it only decides what an already-authenticated request
may do **here**. Two pieces implement that, both configurable at runtime:

| Block | Question it answers |
| --- | --- |
| `identity` | May this process believe the `X-User-*` headers, and who is the principal when it may not? |
| `authorization` | May that principal perform a mutation? |

## Identity

`identity.trusted_headers` is `false` by default. While it is false a header
value is recorded (`identity.AssertedFromContext`) for audit and **never**
becomes a principal, so a caller cannot choose which colleague it claims to be.
Requests then fall back to `identity.service_identity`, marked
`User.Service`.

Turn `trusted_headers` on only after the Gateway strips client-supplied
`X-User-*` headers and injects verified claims. That normalizer is a Gateway
change, not a service change.

`identity.service_identity` is the escape hatch for a cluster whose callers are
all automation, and it is empty by default. Setting it makes **every** request
that carries no trusted user act as that identity, so authorization can no
longer tell automation from an untrusted caller. It is a transitional
convenience, not a substitute for the normalizer.

## Authorization

`authorization.mode` has three values:

- `off` — pre-existing behaviour. Every request is allowed and nothing is
  recorded.
- `observe` — every request is allowed, and a decision that *would* deny is
  logged with `mode=observe`. This is the rollout step.
- `enforce` — the request is denied: `401` when no principal was established,
  `403` when the principal is not allowed to mutate.

Reads (`GET`/`HEAD`/`OPTIONS`) are always allowed; they are the UI's data path
and the Gateway already authenticated the caller. Mutations need a principal
listed in `authorization.allow_principals`, as an exact address or a `*@domain`
pattern. An empty list allows nobody, so a missing configuration denies rather
than opens.

Rollout: deploy with `observe`, check the `authorization would deny` log lines
for a full publish/rerun cycle, then set `enforce`.
