# GCS Bazel Cache CAS GC v3

## Summary

`CAS GC v3` changes the cleanup direction from CAS-driven to AC-driven
cascading cleanup.

The main goal is still to delete CAS objects, because CAS is the dominant source
of bucket size. The v3 safety rule is:

```text
delete cold AC first
then delete only CAS exposed by those AC objects
only when CAS is extra cold
and no AC reference remains in the reference index
```

This is intentionally more conservative than deleting CAS by LRU alone. `CAS
last_seen_at` is not updated when Bazel reads an AC entry, so CAS age by itself
does not prove that no AC still references it.

## Retention

Initial production defaults:

```text
AC retention days:      10
CAS retention days:     15
Safety buffer days:      1
Effective AC cutoff:    11 days idle
Effective CAS cutoff:   16 days idle
```

The important property is:

```text
effective_cas_idle_days > effective_ac_idle_days
```

The extra CAS idle interval is a guardrail. It reduces risk, but it does not
replace the AC reference check.

## Persistent Tables

v3 continues to use the minimal reference index from v2:

```text
gcs_cache_ac_cas_references
- ac_object_name STRING
- cas_object_name STRING
```

```text
gcs_cache_ac_reference_index_state
- shard INT64
- indexed_through TIMESTAMP
```

There is still no persistent run history table. Per-run candidate and reference
snapshot tables use short TTLs.

## Per-Run Temporary Tables

Each delete run materializes:

```text
candidate_ac
run_ac_cas_refs
ac_live_metadata
ac_missing_metadata
delete_ac
candidate_cas
cas_live_metadata
cas_missing_metadata
delete_cas
```

`run_ac_cas_refs` is created before AC deletion. It preserves the CAS set exposed
by the selected AC objects even after successful AC reconcile removes persistent
reference rows.

## Delete Flow

1. Verify all AC reference index shards are ready and fresh.
2. Select cold AC from `gcs_cache_object_last_seen_current`.
3. Materialize `run_ac_cas_refs` from `gcs_cache_ac_cas_references`.
4. Resolve live AC metadata and generation.
5. Reconcile missing AC out of `last_seen_current` and the reference index.
6. Delete live AC by Storage Batch Operations with `bucket,name,generation`.
7. Reconcile successfully deleted AC from `last_seen_current` and the reference
   index.
8. Build CAS candidates from `run_ac_cas_refs`.
9. Keep only CAS with `last_seen_at < now - (cas_retention + buffer)`.
10. Exclude any CAS that still has a remaining AC reference.
11. Resolve live CAS metadata and generation.
12. Reconcile missing CAS from `last_seen_current`.
13. Run raw audit recheck in the final CAS delete table.
14. Delete CAS by Storage Batch Operations with `bucket,name,generation`.
15. Reconcile successfully deleted CAS from `last_seen_current`.

If AC delete fails or partially fails, the job stops before CAS deletion.

If the reference index is missing or stale, the job fails closed.

## Safety Boundary

This is unsafe:

```text
CAS idle for 16 days => safe to delete
```

This is the v3 condition:

```text
CAS is referenced by AC selected in this run
AND selected AC has been deleted or is already missing
AND CAS idle is beyond the extra CAS cutoff
AND no AC reference remains in gcs_cache_ac_cas_references
AND raw audit has no later CAS get/create event
AND live GCS generation still matches
```

The outside-reference check is the main protection against known dangling AC.
The extra CAS idle interval is an additional risk reducer.

## Rollout

1. Keep old CAS LRU delete disabled.
2. Run dry-run and inspect:
   - selected cold AC count
   - CAS referenced by selected AC
   - CAS surviving the extra idle guardrail
   - CAS blocked by remaining AC references
3. Run a small AC + CAS canary with a small CAS cap.
4. Watch CI for `Missing digest` failures.
5. Increase CAS delete cap gradually only after stable canaries.

## Operational Defaults

```text
COST_INSIGHT_GCS_CACHE_AC_RETENTION_DAYS=10
COST_INSIGHT_GCS_CACHE_CAS_RETENTION_DAYS=15
COST_INSIGHT_GCS_CACHE_SAFETY_BUFFER_DAYS=1
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_OBJECTS=10000000
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_CAS_OBJECTS=500
```
