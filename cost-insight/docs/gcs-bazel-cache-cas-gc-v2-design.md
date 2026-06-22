# GCS Bazel Cache CAS GC v2

## Summary

`CAS GC v2` replaces the previous steady-state `AC LRU + CAS LRU` model with a
CAS-driven cascading GC:

1. Build and maintain a persistent `AC -> CAS` reference index in BigQuery.
2. Select cold CAS objects from `gcs_cache_object_last_seen_current`.
3. If a cold CAS is still referenced by any AC, delete those AC objects first.
4. Only delete CAS objects that were already unreferenced at the start of the
   run.
5. On the next daily run, the CAS objects whose ACs were deleted in the
   previous run can become eligible for deletion.

This design avoids the failure mode we saw in v1: deleting CAS directly while a
still-live AC continues to point to it, which leads to `Missing digest` cache
errors in CI.

## Persistent Tables

### `gcs_cache_ac_cas_references`

```text
ac_object_name STRING
cas_object_name STRING
```

This is the only persistent reachability index. One row means a live AC object
currently references one CAS object.

### `gcs_cache_ac_reference_index_state`

```text
shard INT64
indexed_through TIMESTAMP
```

This table always contains `256` rows, one per shard. `indexed_through` stays
`NULL` until that shard has been bootstrapped successfully.

Cleanup must fail closed unless all `256` shards have non-`NULL`
`indexed_through`.

## Bootstrap And Incremental Sync

The new CLI command is:

```bash
cost-insight sync-gcs-cache-ac-references --mode bootstrap|incremental
```

### Sharding

Shards are derived from the first byte of the AC digest:

```text
TO_CODE_POINTS(FROM_HEX(SUBSTR(object_name, 4, 2)))[OFFSET(0)]
```

That gives a stable `0..255` shard id for AC object names like
`ac/<64-hex-digest>`.

### Bootstrap mode

Bootstrap reads all currently tracked AC objects from
`gcs_cache_object_last_seen_current`:

- `object_kind = 'ac'`
- selected by shard

For each AC object:

1. Download the live `ac/...` blob from GCS.
2. Parse it as `ActionResult`.
3. Extract direct CAS references from:
   - `output_files`
   - `stdout_digest`
   - `stderr_digest`
4. Extract indirect references from output directories:
   - `tree_digest`
   - `root_directory_digest`
   - nested `Directory` / `Tree` file nodes
5. Atomically replace the AC's reference rows in
   `gcs_cache_ac_cas_references`.

Only after the whole shard succeeds do we advance that shard's
`indexed_through` watermark.

### Incremental mode

Incremental sync reads only `storage.objects.create` events for AC objects after
the shard watermark and up to the current run start time.

If the same AC name was overwritten multiple times in the window, the job still
downloads only the current live AC object and replaces its references with the
latest live state.

If an AC object is already missing at sync time, its reference rows are cleared
by replacement with an empty edge set.

## Cleanup Flow

The cleanup entry point remains:

```bash
cost-insight cleanup-gcs-cache --mode dry-run|delete --execute-kind all|cas
```

`all` is now just an alias of `cas` for dry-run compatibility. Real delete only
supports `--execute-kind cas`.

### Retention

- CAS retention comes from `GCS_CACHE_CAS_RETENTION_DAYS`
- safety buffer comes from `GCS_CACHE_SAFETY_BUFFER_DAYS`
- effective cutoff is:

```text
last_seen_at < now - (cas_retention_days + safety_buffer_days)
```

There is no independent AC LRU delete anymore.

### Delete sequence

For one delete run:

1. Verify all AC reference index shards are ready.
2. Materialize a seed table of cold CAS candidates, ordered by oldest
   `last_seen_at`.
3. Split the seed into:
   - AC candidates: distinct ACs that still reference any seeded CAS
   - CAS candidates: seeded CAS objects that had no AC references at seed time
4. Resolve live GCS metadata for both sides and capture `generation`.
5. Reconcile already-missing AC / CAS objects out of BigQuery state.
6. Delete live AC candidates first, using `bucket,name,generation` manifests.
7. After AC delete succeeds, remove:
   - AC reference rows
   - AC rows from `gcs_cache_object_last_seen_current`
8. Build the final CAS delete table from:
   - seed-time unreferenced CAS candidates
   - live generation metadata
   - exact `last_seen_at` match back to `gcs_cache_object_last_seen_current`
   - raw audit recheck ensuring there was no later `storage.objects.get` or
     `storage.objects.create`
9. Delete CAS via `bucket,name,generation` manifest.
10. Reconcile deleted CAS rows from `gcs_cache_object_last_seen_current`.

Important: CAS objects that were referenced at the beginning of the run are not
deleted in the same run, even if their ACs are deleted successfully later in
that run. They are left for the next daily run.

## Safety Properties

### Why AC first

If a cold CAS is still reachable from a live AC, deleting the CAS first can
leave a broken action cache entry behind. Deleting the AC first avoids that
dangling reference.

### Why generation is required

Both AC and CAS manifests include `generation`, not just `bucket,name`.

That means if an object is overwritten between candidate selection and actual
delete execution, Storage Batch Operations will target only the intended live
version. A newly created replacement object is protected.

### Why raw audit recheck exists for CAS

`gcs_cache_object_last_seen_current` is updated by daily summarization, not
continuously. Before a CAS delete manifest is exported, the final CAS candidate
table rechecks raw audit logs and rejects any object with later
`storage.objects.get` or `storage.objects.create` activity.

## CLI And Config Additions

### New config

- `COST_INSIGHT_GCS_CACHE_AC_CAS_REFERENCES_TABLE`
- `COST_INSIGHT_GCS_CACHE_AC_REFERENCE_INDEX_STATE_TABLE`
- `COST_INSIGHT_GCS_CACHE_AC_REFERENCE_SHARD_COUNT`
- `COST_INSIGHT_GCS_CACHE_AC_REFERENCE_BATCH_SIZE`
- `COST_INSIGHT_GCS_CACHE_AC_REFERENCE_DOWNLOAD_WORKERS`

### New CLI

```bash
cost-insight sync-gcs-cache-ac-references --mode bootstrap --shard-start 0 --shard-end 15
cost-insight sync-gcs-cache-ac-references --mode incremental --shard-start 0 --shard-end 63
```

## Rollout Plan

1. Run bootstrap shard canary on `1/256` shard and measure throughput.
2. Run full bootstrap across all `256` shards.
3. Verify `gcs_cache_ac_reference_index_state` has no `NULL`
   `indexed_through`.
4. Run daily incremental sync for several days.
5. Run cleanup dry-run with CAS GC v2 and inspect:
   - cold CAS candidate count
   - AC cascade count
   - directly deletable CAS count
6. Run a small delete canary.
7. Replace the old steady-state AC delete cronjob with:
   - daily incremental AC reference sync
   - daily CAS cascade cleanup

## Current Implementation Notes

The implementation in `cost-insight` now includes:

- protobuf-wire parsing for Bazel `ActionResult`, `Tree`, and `Directory`
- `sync-gcs-cache-ac-references`
- CAS-driven cascade cleanup in `cleanup-gcs-cache`
- generation-aware delete manifests for both AC and CAS
- fail-closed cleanup readiness checks on the AC reference index

The old `mixed-canary` delete path and independent AC delete semantics are not
part of v2.
