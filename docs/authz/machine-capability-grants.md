# Machine capability grants: first reviewed baseline

`authzd/policies/machine-capability-grants.json` holds the capability tuples for
the machine callers that already reach TiBuild. Its purpose is to mirror today's
behaviour exactly, so that turning on enforcement later is not also a policy
change.

## What it contains

Six tuples: three capabilities × two callers.

| Capability object | Relation written | Checked by the registry as |
| --- | --- | --- |
| `capability:tibuild.devbuild.rerun` | `caller` | `#invoke` |
| `capability:tibuild.devbuild.create` | `caller` | `#create` |
| `capability:tibuild.devbuild.read` | `caller` | `#read` |

Tuples are always written on `caller`; `invoke`, `create` and `read` are derived
relations in the model and cannot be written directly.

Callers come from the machine route's service-account allowlist
(`apps/gcp/tibuild/v2/machine-access.yaml`). Every allowlisted account can call
every machine endpoint today, so every one of them gets all three capabilities.
Tightening that is a product decision, deliberately not taken here.

| Service account | IAM uniqueID | Subject |
| --- | --- | --- |
| `tcms-tibuild-client@pingcap-testing-account` | `106613601288865186001` | `workload:google/sub/106613601288865186001` |
| `tirelease-tibuild-client@pingcap-testing-account` | `108356868865109323956` | `workload:google/sub/108356868865109323956` |
| `devops-server@gcp-resilience-dev` | `102958450220350375744` | `workload:google/sub/102958450220350375744` |
| `devops-server-dev@gcp-resilience-dev` | `102659811527506589949` | `workload:google/sub/102659811527506589949` |
| `devops-server-staging@gcp-resilience-dev` | `102079013862015475144` | `workload:google/sub/102079013862015475144` |

The first row cross-checks with a decision log: TiBuild calls arrive with
`principal=workload:google/sub/106613601288865186001`, which is that service
account's IAM uniqueID. That is the service-account equivalent of Gate 1, verified
against production rather than assumed.

## What used to be missing, and what it means now

The three `gcp-resilience-dev` callers were absent because this project could not
read that project's IAM. Their uniqueIDs were supplied by someone with access, so
the file now covers all five allowlisted callers. Resource checks are no longer a
gap: ADR 0005 was accepted, so the registry rules
carry a capability check only and TiBuild keeps object ownership. Enforcement is
therefore blocked only by the decision to flip the mode, and by confirming that
every caller the allowlist names is in this file — an allowlisted caller without a
tuple would be denied the moment `enforce` is on.

Do not read a green `relationship_denied` count as readiness to enforce: it only
says the caller lacks the capability.

## Applying it

Dry-run against the real store first:

```sh
go run ./cmd/tuple-sync \
  --snapshot authzd/policies/machine-capability-grants.json \
  --object-type capability --relation caller \
  --dry-run
```

2026-11-26 result against the GCP store: `outcome=planned, desired=6, writes=6,
deletes=0`.

Then drop `--dry-run`. The file is authoritative for `capability#caller`: a tuple
in that scope that is not in the file is deleted on the next run. That is why the
file must be reviewed and why the missing callers matter — after the two known
callers are written, a file that still lacks the other three does not remove
anything today, but the moment someone adds them by hand this file takes them
away again.

## Store behaviour worth knowing

The deployed OpenFGA rejects a type-only read (`{"tuple_key":{"object":"capability:"}}`
returns `the object type field is required`). `TupleStore.Read` therefore reads
without a filter and selects the object type client-side. That is more traffic
than a filtered read and is bounded by the store's size; it is recorded in
`docs/authz/tuple-sync.md` so the next reader does not re-learn it.
