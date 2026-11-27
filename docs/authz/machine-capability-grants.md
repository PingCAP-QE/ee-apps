# Machine capability grants: first reviewed baseline

Ten tuples: two capabilities × five callers, for the machine callers that already
reach TiBuild.
Their purpose is to mirror today's behaviour exactly, so that turning on
enforcement later is not also a policy change.

**The real file is not stored in this repository.** It maps service accounts to
OpenFGA subjects, which is the authorization topology — needed by the job, not by
anyone reading the code. It is maintained in the private operations vault and
applied by the job from a mounted file. This repository keeps
`authzd/policies/machine-capability-grants.example.json`, which shows the exact
shape with placeholder subjects.

## What it contains

Two capabilities per caller, one tuple each. Rerun and create share a capability:
the gateway decides that a caller may create builds, and TiBuild decides whether a
particular build may be rerun by that person.

| Capability object | Relation written | Checked by the registry as |
| --- | --- | --- |
| `capability:tibuild.devbuild.create` | `caller` | `#create` (create and rerun) |
| `capability:tibuild.devbuild.read` | `caller` | `#read` |

Tuples are always written on `caller`; `invoke`, `create` and `read` are derived
relations in the model and cannot be written directly.

Callers come from the machine route's service-account allowlist
(`ee-ops` `apps/gcp/tibuild/v2/machine-access.yaml`). Every allowlisted account can
call every machine endpoint today, so every one of them gets all three
capabilities. Tightening that is a product decision, deliberately not taken here.

A caller's subject is `workload:google/sub/<IAM uniqueId>`, derived with:

```sh
gcloud iam service-accounts describe <service-account> \
  --project=<project> --format='value(uniqueId)'
```

That mapping, and the file built from it, live in the private operations vault.
Service-account emails are public already — they appear in the `ee-ops` allowlist —
but the id mapping is not something a reader of this repository needs, and it is
one half of the authorization topology.

Gate 1 applies here too, in its service-account form: a decision log must show the
same numeric id as the principal. That was verified against production traffic for
one caller before the rest were added.

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

Dry-run against the real store first, with the file mounted from the private
source (`--snapshot <path>`). The job does not care where the file comes from; what
matters is that the reviewed content, not a local edit, is what gets applied:

```sh
go run ./cmd/tuple-sync \
  --snapshot /etc/authzd/machine-capability-grants.json \
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
