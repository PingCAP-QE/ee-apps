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
A refused takeover must not overwrite another owner's `last_error`: the saved
checkpoint error is preserved; find the current refusal reason in the contender's
Pod logs. Progress logs include `recovered_from_run_id` for recovery audit.

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
  Configure `restartPolicy: Never` so a retry creates a new Pod/UID. `OnFailure`
  restarts the same Pod/UID and does not support automatic reclaim; increasing
  retries cannot fix that. A new Pod still must validate the previous owner.
- Read-only preflight and local tests do not establish production recovery.
  Verify the actual terminal run state, every recovered day, active serving
  pointers, all four monetary fields and the source freshness result.

## Scope changes and failed checkpoints

Even a valid change to the account list, project or `source_profile` is rejected
if it differs from the saved checkpoint scope. Do not discard that checkpoint to
bypass the guard. `completed=[]` does not prove that no writes occurred.

First suspend/coordinate the affected worker and prevent concurrent CronJob or
manual runs. Confirm that the old owner has exited; lock TTL alone is not proof.
Finish and verify every pending day under the original scope, or obtain explicit
approval for a migration/repair that reconciles those days before clearing state.
Do not abandon unresolved days when switching scope.

Use this bounded, read-only diagnostic query:

```sql
SELECT job_name, last_status, last_error, watermark_json,
       JSON_UNQUOTE(JSON_EXTRACT(watermark_json, '$.run_id')) AS run_id
FROM cost_job_state
WHERE job_name IN ('sync_azure_warehouse_daily',
                   'sync_aws_warehouse_daily',
                   'sync_aws_f04_warehouse_daily');
```

Only after the above reconciliation and explicit approval may an operator
acknowledge a **failed** checkpoint with the transaction below. This is a
controlled GKE operation, not routine recovery or automatic lock clearing.
Never use it for `running` state. Keep the worker paused until the transaction
ends. Bind `:job_name` to exactly one of the three jobs above, `:old_run_id` to
the approved checkpoint's run ID, and `:expected_status` to `failed`, using a
SQL client/driver that supports bound parameters (not string interpolation).

```sql
START TRANSACTION;
SELECT job_name, last_status, last_error, watermark_json,
       JSON_UNQUOTE(JSON_EXTRACT(watermark_json, '$.run_id')) AS run_id
FROM cost_job_state
WHERE job_name = :job_name
FOR UPDATE;
-- Recheck the locked row against the approved scope, old run ID and failed
-- status. If absent or different, ROLLBACK and stop; do not run the UPDATE.

UPDATE cost_job_state
SET last_status = 'ready', watermark_json = '{}', last_error = NULL
WHERE job_name = :job_name
  AND last_status = 'failed'
  AND last_status = :expected_status
  AND JSON_UNQUOTE(JSON_EXTRACT(watermark_json, '$.run_id')) = :old_run_id;
SELECT ROW_COUNT() AS affected_rows;
-- COMMIT only if affected_rows is exactly 1; otherwise ROLLBACK.
-- Do not leave this transaction open while deciding what to repair.
```

This acknowledges the reconciled checkpoint, not a successful run:
`last_succeeded_at` is deliberately unchanged.

## Deployment

Application recovery code and runtime permissions must be reviewed together.
The companion `ee-ops` change adds only `get` on Pods in `apps`, plus downward
Pod namespace/UID fields. It must not grant Pod mutations, secret access or
cluster-wide permissions. Publishing a code change alone does not alter the
image already configured on a CronJob.
