# Relationship tuple sync

`authzd` decides authorization from OpenFGA tuples. This document records which
system owns each tuple family, how the sync job proves it read a complete
snapshot, and how operators observe revocation delay. It is the contract that
[ADR 0005](adr/0005-tibuild-resource-ownership.md) requires before a tuple writer
is enabled.

Nothing here authorizes traffic: `authzd` route modes remain `off` until the
identity handoff, model and route policy are reviewed and a route is verified
through the Gateway.

## Owned tuple families

| Tuple family | Owner | Status | Snapshot source |
| --- | --- | --- | --- |
| `capability:*#caller` | This repository's reviewed grants file | Implemented | Reviewed JSON snapshot, revision = commit SHA |
| `team:*#member` | Not decided | Open | Requires a read-only contract with the directory owner |
| `organization:*#admin` | Not decided | Open | Same as teams |
| `project:*#owner\|editor\|viewer` | Not decided | Open | Requires the owning system for project membership |
| `devbuild:*#parent\|creator` | TiBuild | Blocked | No immutable build-to-project relation exists today (ADR 0005) |

The reviewed grants file is authoritative **for its own scope** because the file
is the complete membership of that scope by construction: it is reviewed in Git,
its revision is the commit that produced it, and a reconciler owns exactly the
object types and relations the file declares. No other system may write the same
`capability:*#caller` tuples; a manual grant in that scope is drift and the next
run removes it.

Team, organization and project membership are deliberately unimplemented. Using
the `roster` employee and group tables as an authorization source would turn
organizational reporting data into access grants, which is a product decision
that needs the service owner's written approval, and `roster` also has no
Google `sub` mapping. Confirmed rebuilds are not a substitute for a reviewed
ownership contract.

## Snapshot contract

A snapshot has five required properties:

| Field | Rule |
| --- | --- |
| `source` | Names the authoritative system. Required, non-empty. |
| `revision` | The reviewed revision the data came from. Required; recorded in the decision audit trail. |
| `generated_at` | When the source produced the data. Required; the reconciler reports observation lag from it. |
| `complete` | `true` only when the read covered the whole scope. An omitted or false value never prunes. |
| `tuples` | The full desired membership of the scope. Every entry must be a valid tuple inside the declared scope. |

Rejection rules, all fail-closed and none of which mutate the store:

- an unknown JSON field, a trailing value, or a file above 1 MiB is rejected —
  a partially understood snapshot must not become a membership list;
- any malformed tuple fails the whole snapshot, so a typo cannot silently drop a
  grant;
- any tuple outside the declared object types or relations fails the whole
  snapshot, so a job cannot prune tuples it does not own;
- an incomplete snapshot is refused before the current tuples are read;
- a snapshot that would delete every owned tuple is refused unless the operator
  passed `--allow-empty` after confirming the source is empty rather than failed.

An empty read is never treated as an authoritative empty membership list. A
source that cannot distinguish "no members" from "read failed" must report
`complete: false`.

## Reconciliation

`cmd/tuple-sync` computes the difference between the snapshot and the tuples the
job owns, then applies it:

1. deletes run before writes, in batches of 100, with the authorization model ID
   pinned on every request;
2. a second run against the same snapshot issues no request;
3. tuples outside the scope are neither read for the diff nor touched.

The job is one-shot: run it from a reviewed change pipeline (after the grants
file merges) or a scheduled `CronJob`. It never runs inside request-serving code,
so a slow or failing OpenFGA cannot affect decision latency.

### Reading the store

The deployed OpenFGA rejects a type-only read: `{"tuple_key":{"object":"capability:"}}`
answers `validation_error: the object type field is required and both the object id
and user cannot be empty`. An empty body returns every tuple, so the store reads
unfiltered and selects the object type client-side. Expect the read volume to
follow the whole store, not the owned scope, and keep that in mind before the store
grows.

## Observability

Every run prints Prometheus text and one JSON log line:

| Metric | Meaning |
| --- | --- |
| `authzd_tuple_sync_total{outcome=...}` | `applied`, `planned`, `noop`, `incomplete_snapshot`, `empty_snapshot_refused`, `invalid_snapshot`, `invalid_tuple`, `out_of_scope_tuple`, `error` |
| `authzd_tuple_sync_last_success_timestamp_seconds` | Last successful reconcile; alert when it stops advancing |
| `authzd_tuple_sync_lag_seconds` | Age of the snapshot at the last successful run — the operator-visible revocation delay |
| `authzd_tuple_sync_writes_total` / `authzd_tuple_sync_deletes_total` | Cumulative applied changes |

Revocation has two independent delays: the time for a reviewed removal to reach
the snapshot (change review plus job scheduling) and the time for the store to
reflect it (one job run). `authzd` reads sensitive mutations with higher
consistency, which covers the second delay only; OpenFGA consistency cannot fix a
snapshot that was never regenerated. Alert on `authzd_tuple_sync_lag_seconds`
alongside the `incomplete_snapshot` and `error` outcomes.

## Running it

```sh
AUTHZD_OPENFGA_URL=http://openfga:8080 \
AUTHZD_OPENFGA_STORE_ID=<store ULID> \
AUTHZD_OPENFGA_MODEL_ID=<pinned model ULID> \
go run ./cmd/tuple-sync \
  --snapshot /etc/authzd/grants.json \
  --object-type capability \
  --relation caller \
  --dry-run
```

`--dry-run` reports `outcome="planned"` with the write and delete counts and
does not touch the store. Drop it to apply. `AUTHZD_OPENFGA_API_TOKEN` is
optional and comes from secret management. The process must run as the dedicated
sync identity: it needs read and write on the store, and no human workload should
share those credentials.

## Open decisions

1. The owning system and read-only access contract for team, organization and
   project membership, plus the stable identity keys and how removals are
   observed.
2. The Google `sub` to employee mapping. The ID token carries `sub` and a
   verified email, but no approved mapping export exists yet; email must not
   become the primary key.
3. Whether `devbuild` parent and creator tuples can ever be populated from
   TiBuild data (ADR 0005 keeps resource enforcement off until then).
