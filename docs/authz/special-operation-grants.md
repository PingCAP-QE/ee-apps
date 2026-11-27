# Granting a special operation to one person

Decision (2026-11-26): authorization for special operations is **per individual**.
No group lookup, no directory sync, no team object. A person gets a capability
because someone added a tuple naming them, and loses it when that tuple is removed.

Browser reads are unaffected: any authenticated employee may read (ADR 0002), so
reads need no tuples at all.

## Why per individual

| Requirement | Effect on the design |
| --- | --- |
| Google ID tokens carry no groups | Nothing to key a team membership on without an extra privileged integration |
| Special operations are rare and named | A reviewed list of people is small enough to maintain by hand |
| Access should be attributable | A tuple per person makes "who was granted this, and when" answerable from the file's history |

A group would be cheaper to maintain and would silently widen when someone adds a
member for an unrelated reason. For a handful of privileged operations, the manual
list is the safer trade.

## Tuple shape

```
capability:<operation>#caller@user:google/sub/<sub>
```

`caller` is the only writable relation; `invoke`, `read`, `create` resolve to it.

That has a consequence for naming: **one tuple on `caller` grants every alias of
that object**, so a privilege level needs its own object. That is why reads live on
`capability:tibuild.devbuild.read` and writes on
`capability:tibuild.devbuild.create`; putting both actions on one object would
make a read grant imply a write grant. Operations that differ only in business
policy — creating a build versus rerunning someone else's — can share an object,
because the service decides the rest.
The subject is the person's Google ID-token `sub`, taken from their own decision
log line (`principal=user:google/sub/<sub>`), never from an email address.

Example, for the capability a privileged endpoint checks:

```json
{"user":"user:google/sub/<sub>","relation":"caller","object":"capability:tibuild.publish.images"}
```

## Where the file lives

The reviewed grants file is kept in the private operations vault, not in this
repository: it maps real people to real subjects, which is the authorization
topology. This repository carries the shape and the reasoning.

A single file can hold both kinds of caller — `workload:google/sub/...` for
service accounts and `user:google/sub/...` for people — because the reconciler only
compares tuples, not subject kinds. Keep one file so one review covers both.

## Operations

1. Add or remove one tuple in the reviewed file, with a commit message naming the
   operation and the reason.
2. Run the sync with `--dry-run` and read the plan: it must be exactly the intended
   insert or delete.
3. Apply, then confirm the next decision for that person reports `authorized`
   (`would_deny=false`) or `relationship_denied`.

Revocation is the same flow in reverse. There is no caching on the read path beyond
the store itself, so the next decision after the sync reflects the change.

## What this does not provide

- **No automatic provisioning.** Nobody is granted anything by being hired,
  transferred or added to a chat group. That is deliberate; it means a stale list
  grants a stale privilege, so the file needs an owner who reviews it.
- **No "everyone in X" grants.** If a future operation needs that, it is a new
  decision — the group path is documented and dormant, not deleted.
- **No per-object checks.** TiBuild keeps object ownership (ADR 0005); a capability
  grant says a person may use an operation, not that they may use it on a
  particular resource.
