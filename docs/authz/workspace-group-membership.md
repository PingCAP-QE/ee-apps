# Workspace group membership as the read-visibility source

Decision: "every authenticated employee" is a Google Workspace group, and its
membership is the only source for the `team` object that grants browser reads.
This records the API, the credential, the mapping that must be verified before the
first real run, and what happens on revocation.

The reader side is implemented in `authzd/internal/workspace`: a
`relationships.Source` that reads one group and returns a complete snapshot. The
reconciler that applies it already exists and is unchanged.

## Why this source

| Requirement | How a Workspace group meets it |
| --- | --- |
| One place to answer "who is staff" | The group is a Workspace object with its own owner and change history |
| Stable subjects | The Directory returns the numeric user id, which should equal the ID token `sub` (gate 1 below) |
| Revocation | Removing a member from the group is the single action that must remove access |
| Review | Membership changes are visible in the Workspace admin log, not in a repository |

Rejected alternatives, recorded so the choice is not re-litigated: `roster` (Lark
derived, no Google `sub` mapping, organizational reporting data), and a reviewed
static list (exact but does not scale and goes stale silently).

## API and credential

Read `https://admin.googleapis.com/admin/directory/v1/groups/{groupKey}/members`
with `roles=MEMBER`, following `nextPageToken`. Exactly one page sequence becomes
one snapshot; if any page fails, `Fetch` returns an error and the reconciler keeps
the previous state.

Credential options, both requiring a Workspace administrator:

1. **Service account with domain-wide delegation**, scoped to
   `https://www.googleapis.com/auth/admin.directory.group.member.readonly`, and
   the token minted for an admin subject. Preferred: no interactive token.
2. An OAuth client authorized by an admin with the same scope. Works, but needs a
   refresh-token owner.

Either way the job must run as its own identity, separate from the `authzd`
decision path, and that identity (not `authzd`) holds the OpenFGA write
credential for tuple sync.

## Gate 1 — verify the id mapping before the first run

Tuples are written as `user:google/sub/<member id>`, which is only correct if the
Directory `user.id` equals the ID token `sub`. That is not assumed:

1. take a known employee's `sub` from a decision log (we have one:
   `user:google/sub/102401699953594095413`);
2. read the same user from the Directory API and compare its `id`;
3. if they differ, stop: the subject must be derived from the `sub` claim, and the
   directory lookup needs a mapping table instead.

Numbers are compared as text. Ids are 21 digits and overflow a 64-bit integer —
that mistake is already covered by a test.

## Tuples

| Tuple | Written by |
| --- | --- |
| `team:<staff-group>#member@user:google/sub/<id>` | this sync |
| `capability:tibuild.devbuild.read#caller@team:<staff-group>#member` | reviewed grants file |

The group is the grant of *capability*, not of data: `capability:tibuild.devbuild.read`
resolves to `caller`, and the read rules in the registry check only that
capability (see `docs/authz/browser-read-authorization.md`). Row filtering stays in
the data layer.

## Completeness and revocation

- The snapshot sets `Complete` only after the last page. A partial read is an
  error, never a smaller member list — the reconciler would otherwise delete the
  members it failed to see.
- An empty group yields a complete snapshot with zero tuples. The reconciler
  refuses to delete every owned tuple in that case unless an operator opts in,
  because "the group is genuinely empty" and "the read returned nothing" look
  identical from the data.
- Revocation has two delays: the wait until the next sync reads the group, and the
  time the reconciler needs to apply. `authzd_tuple_sync_lag_seconds` exposes the
  first; a decision for a removed member can therefore succeed until the next run.
  Sensitive mutations already request higher consistency; reads do not, so the
  poll interval is the real bound and should be chosen with that in mind.

## Scope constraint

The reconciler owns a slice defined by object types and relations, so this source
owns **all** `team#member` tuples. Two consequences:

- every team whose membership matters must come from the same group-read mechanism,
  or two sources will fight over the same relation;
- if a future team needs a different source, `Scope` has to gain per-object
  granularity first. That is a small change, but it must happen before the second
  source exists, not after.

## Running it

The job reads the Directory through the same `cmd/tuple-sync`, with a token file
instead of a reviewed snapshot. The token file holds a bearer token with the
Directory scope and is re-read on every call, so a rotated token needs no restart.

Run Gate 1 first, with a credential that can read the Directory:

```sh
go run ./cmd/tuple-sync \
  --source workspace-group \
  --workspace-group <group-key> \
  --team-object team:all-staff \
  --workspace-token-file /etc/authzd/workspace.token \
  --verify-email <a-known-employee> \
  --verify-subject <their sub from a decision log> \
  --object-type team --relation member \
  --dry-run
```

A mismatch aborts before the store is touched, so a wrong mapping cannot write a
single tuple. Keep both `--verify-*` flags on the first real runs; they cost one
extra API call and they are the only thing that catches an id/subject divergence
introduced later.

`--directory-url` exists for tests against a stub; leave it unset in a cluster.

Without the verification flags the same command performs the sync. Use `--dry-run`
first to see the plan, then drop it. The job needs its own OpenFGA credential with
write access to the store — never the `authzd` decision-path credential.

## Open items

1. Which group, and who owns its membership. Contractors or guests in that group
   get read access to every build — that is the semantic of the choice and needs an
   explicit owner sign-off.
2. The credential (option 1 or 2) and the administrator who grants it.
3. Gate 1 above, once a directory-readable credential exists.
4. Nested groups are not expanded: the implementation reads direct members and
   ignores `type=GROUP` entries. If the chosen group is a container of groups, use
   Cloud Identity transitive membership instead and record that change here.
