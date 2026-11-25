# Warehouse checkpoint resume operations

## Recovery unit

A warehouse worker completes one account/usage-day at a time:

1. replace only the queried billing-partition/day summary;
2. refresh that day's attribution;
3. publish native resource serving;
4. verify monetary conservation and publication identity;
5. checkpoint the completed account/day.

The checkpoint is the boundary for resuming, not the last SQL statement that
returned. If a process stops during any earlier step, resume the unfinished day
from its deterministic replacement boundary. Do not skip it merely because some
summary rows or a publication already exist. Preserve source-snapshot, currency,
identity, empty-source, protected-partition and monetary checks.

A previous successful partial-day export is not proof of final completeness.
For example, on 2026-11-25 Azure usage date Nov21 existed locally at USD 472.74,
while its refreshed source amount was USD 3560.32. A MAX(usage_date) check alone
cannot detect that discrepancy.

## Automatic recovery

A failed invocation retains its original windows and completed account-days.
The next invocation atomically claims that checkpoint and finishes only its
unfinished days, without silently substituting a newer trailing window. Each
recovery pass completes the saved window; the next normal invocation then
continues rolling refresh. This can defer newest usage to a later invocation,
but never makes missing historical days look complete.

For a crashed process still recorded as `running`, the worker checks the recorded
Pod through the Kubernetes API before taking ownership. A live Pod or an unknown
lookup result blocks takeover. The database state is rechecked under lock after
that network lookup so two contenders cannot both reclaim the same checkpoint.
Kubernetes calls are not made while holding the database ownership transaction.
Missing or inconsistent checkpoint identities remain actionable errors, not a
reason to clear every failure. Invalid source data still fails the normal guards.

## Operational safety

- Never clear every `cost_job_state` failure, delete cost facts wholesale, or
  reconnect inside an invalid transaction.
- A recovery must retain its original pending dates even after the normal
  trailing five-day window advances. The incomplete historical day must not be
  silently left behind in order to make the latest watermark look healthy.
- Only one run may own a warehouse operational key. Ownership must be checked
  again when taking over and when saving progress.
- Pod liveness verification requires a namespace-scoped `get pods` permission
  for `cost-insight-gcp-billing`. API permission errors, timeouts or unknown
  ownership are not evidence that an old worker has stopped.
- Kubernetes `backoffLimit` is not the application checkpoint mechanism.
  Repeating a whole Pod without validating the previous owner is not recovery.
- Read-only preflight and local tests do not establish production recovery.
  Verify the actual terminal run state, every recovered day, active serving
  pointers, all four monetary fields and the source freshness result.

## Deployment

Application recovery code and runtime permissions must be reviewed together.
The companion `ee-ops` change adds only `get` on Pods in `apps`, plus downward
Pod namespace/UID fields. It must not grant Pod mutations, secret access or
cluster-wide permissions. Publishing a code change alone does not alter the
image already configured on a CronJob.
