# GCS Bazel Cache CAS GC v3

## Summary

`CAS GC v3` changes cleanup from standalone CAS LRU deletion to AC-driven
cascading cleanup.

The goal is still to reduce CAS storage, because CAS is the dominant source of
bucket size. The important change is that each cleanup run starts from cold AC
objects, parses only those AC objects, and then deletes the CAS objects exposed
by that selected AC set when the CAS objects are even colder.

This version intentionally does not require a full persistent `AC -> CAS`
reference index bootstrap. It is a risk-controlled cleanup strategy, not a
mathematically complete reachability GC.

## Retention

Initial production defaults:

```text
AC retention days:      10
CAS retention days:     15
Safety buffer days:      1
Effective AC cutoff:    11 days idle
Effective CAS cutoff:   16 days idle
```

The key guardrail is:

```text
effective_cas_idle_days > effective_ac_idle_days
```

CAS must be colder than the AC objects selected in the same run. This reduces
the chance that a still-useful CAS object is removed immediately after an AC
entry ages out.

## Persistent Tables

v3 does not add a persistent run history table.

The cleanup job continues to use `gcs_cache_object_last_seen_current` as the
only persistent source for age selection. The existing AC reference index job
can remain disabled for this cleanup path; v3 does not require
`gcs_cache_ac_cas_references` or `gcs_cache_ac_reference_index_state` to be
complete before deleting.

## Per-Run Temporary Tables

Each delete run materializes short-TTL tables:

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

`run_ac_cas_refs` is a per-run snapshot. It is populated by downloading and
parsing the selected AC objects in that run. It is not a persistent global
reference index.

## Delete Flow

1. Select cold AC from `gcs_cache_object_last_seen_current`.
2. Resolve live AC metadata and generation.
3. Download and parse selected AC objects to populate `run_ac_cas_refs`.
4. Reconcile missing AC out of `last_seen_current`.
5. Delete live AC by Storage Batch Operations with `bucket,name,generation`.
6. Reconcile successfully deleted AC from `last_seen_current`.
7. Build CAS candidates from `run_ac_cas_refs`.
8. Keep only CAS with `last_seen_at < now - (cas_retention + buffer)`.
9. Resolve live CAS metadata and generation.
10. Reconcile missing CAS from `last_seen_current`.
11. Run raw audit recheck in the final CAS delete table.
12. Delete CAS by Storage Batch Operations with `bucket,name,generation`.
13. Reconcile successfully deleted CAS from `last_seen_current`.

If AC delete fails or partially fails, the job stops before CAS deletion. If an
individual AC blob cannot be parsed, that AC is skipped for the current run: it
is not deleted, it does not contribute CAS references, and the rest of the run
continues.

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
AND raw audit has no later CAS get/create event
AND live GCS generation still matches
```

This does not prove no other AC object in the bucket references the same CAS.
Instead, it relies on two operational facts:

- AC objects are cheaper to delete and are selected first by age.
- CAS deletion is delayed by a longer retention window and capped during rollout.

The rollout cap is the main blast-radius limiter while we validate CI behavior.

The known residual risk is shared CAS: a cold AC selected in this run and a warm
AC outside this run may both reference the same extra-cold CAS object. v3 does
not run a global outside-reference check, so rollout must keep a small CAS cap
and monitor CI `Missing digest` errors before increasing the cap.

## Dry-Run Semantics

Dry-run remains useful as an AC backlog view. Because v3 only knows referenced
CAS after downloading selected AC objects during a delete run, the dry-run
summary intentionally reports CAS counts as zero.

For CAS impact, use a small real canary cap. The canary performs the same
selection, parsing, manifest export, and Batch Operations flow as production,
but limits CAS deletion volume.

## Rollout

1. Keep old standalone CAS LRU delete disabled.
2. Run dry-run and inspect cold AC backlog.
3. Run a small AC + CAS canary with a small CAS cap.
4. Watch CI for `Missing digest` failures and cache miss amplification.
5. Increase CAS delete cap gradually only after stable canaries.

## Operational Defaults

```text
COST_INSIGHT_GCS_CACHE_AC_RETENTION_DAYS=10
COST_INSIGHT_GCS_CACHE_CAS_RETENTION_DAYS=15
COST_INSIGHT_GCS_CACHE_SAFETY_BUFFER_DAYS=1
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_OBJECTS=10000000
COST_INSIGHT_GCS_CACHE_CLEANUP_MAX_DELETE_CAS_OBJECTS=500
```
