# Scope deliberately not taken

Four surfaces were in the plan and are now closed with a recorded reason. Each
entry says why the gateway does not take it, what still protects it, and what would
reopen the decision. Records exist so "not done" is not mistaken for "forgotten".

## CI Dashboard

**Decision: no gateway object-level authorization.** The Dashboard reads build rows
with query filters, and a gateway allow/deny cannot filter a result set. Route-level
access is already OIDC, and row filtering lives in the application, where the query
exists.

Reopens if the Dashboard needs row-level rules the application cannot express. That
starts with a data-authorization design (which rows, whose, on what basis), not with
a registry rule — a rule without that design would only pretend to protect the rows.

## ChatOps Lark

**Decision: keep the service's own sender checks; no gateway policy.** A Lark
command arrives over a WebSocket from a sender whose identity is in the event
payload, not in an HTTP request header. The gateway never sees that sender, so it
cannot authorize the actor. The service verifies the event signature and checks the
actor per command today, and a service-account entitlement must not be read as
"any Lark sender may do this" — the current checks already separate the two.

Reopens only with a sender-to-canonical-subject mapping, which would be a new
identity integration (Lark user to Google subject). Nothing on the roadmap needs it:
the operations that matter are already reachable through the machine API with a real
identity.

## Zot and third-party UIs

**Decision: inventory only, no migration.** Zot speaks the OCI distribution
protocol; its authorization model is repository-scoped access tokens, and it is
reached as a registry, not as an HTTP API for humans. Adding `extAuth` there would
replace a working native model with a weaker one, and the gateway has no request
shape to map onto OCI operations.

Reopens if a concrete requirement appears that Zot's native model cannot express —
and it would need its own design, not a registry entry.

## OPA / ABAC (the gated phase)

**Decision: not started, no requirement.** The plan made this conditional on a
written ABAC requirement (resource lock state, MFA, trusted network, time windows).
None was produced. The `ContextPolicyEvaluator` hook exists in `authzd` and is
unwired; the model, registry and decision path do not need it.

Reopens only with a written requirement naming the attributes, who owns them, and
what happens when they are unavailable. Building a policy engine before that would
produce rules nobody can review.

## What this leaves

| Surface | Decided by | Recorded in |
| --- | --- | --- |
| TiBuild machine and privileged routes | Gateway capability check + TiBuild ownership | ADR 0005, machine enforce report |
| TiBuild browser reads | Any authenticated employee (ADR 0002) | browser read contract, status note |
| Special operations for individuals | Per-individual capability tuples | special-operation-grants |
| Everything above | No gateway change | this file |
