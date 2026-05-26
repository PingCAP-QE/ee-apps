# CI Dashboard V2 Pod Validation Plan

Status: `sync-pods` canary executed on 2026-04-21, Jenkins pod metadata linkage path verified on samples, full V2.1 review still in progress

Last updated: 2026-04-21

Scope:
- target job:
  - `ci-sync-pods`
- target tables:
  - `ci_l1_pod_events`
  - `ci_l1_pod_lifecycle`
  - `ci_job_state`
- supporting read-only reference table:
  - `ci_l1_builds`
- primary source:
  - Cloud Logging `k8s_pod` event entries

This document is intentionally a pre-execution review draft.
The goal is to agree on validation flow and validation cases before production validation is executed.

Execution note from 2026-04-21 canary:
- `sync-pods` successfully imported real Cloud Logging rows after a temporary explicit access token was injected
- raw event duplicate check passed after a rerun
- recurring enablement is still blocked because the default runtime identity returned Cloud Logging `403 PERMISSION_DENIED`
- build linkage coverage from that canary was only partial because `ci_l1_builds` was about 2 hours behind the newest pod events
- separate manual validation established that sampled GCP Jenkins builds can be linked by pod annotations and labels, without using Jenkins console lookup as the primary path

## 1. Validation Objectives

This validation round focuses on five questions:

1. Are pod events being imported correctly and idempotently?
2. Are Jenkins GCP pod annotations and labels being resolved into the correct build identity?
3. Are lifecycle rows being derived correctly from imported events?
4. Are lifecycle rows linking back to builds with acceptable quality across both linkage paths?
5. Is the recurring sync operationally safe enough to enable on the planned 10-15 minute cadence?

## 2. Validation Principles

- validate from low-level to high-level
- prefer read-only checks first
- keep every validation scope explicit
- separate structural checks from sample-based checks
- rerun/idempotency validation is the only phase that intentionally re-executes the sync job

Read-only by default:
- all SQL checks in Phases 0 through 3 are read-only
- Phase 4 reruns project-owned sync jobs, which write only project-owned tables

## 3. Validation Case Matrix

## 3.1 Group A: Schema And Object Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `POD-A01` | schema presence | required pod tables exist | `SHOW TABLES` / `SHOW CREATE TABLE` | all required tables present |
| `POD-A02` | schema shape | columns and keys match SQL files | `SHOW CREATE TABLE` diff against migrations | no missing required key/index/column |
| `POD-A03` | job state row | `ci_job_state` contains `ci-sync-pods` after first successful run | query `ci_job_state` | row exists and status is coherent |

## 3.2 Group B: Raw Event Import Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `POD-B01` | freshness | newest `receive_timestamp` is recent enough | max timestamp query | lag is within expected operating tolerance |
| `POD-B02` | source identity uniqueness | no duplicate source events after import | group-by duplicate query on source key | zero duplicate source identities |
| `POD-B03` | reason scope | only expected reasons are imported in V2.1 | distinct `event_reason` query | no unexpected reason unless explicitly accepted |
| `POD-B04` | namespace scope | imported namespaces match expected CI namespaces | distinct namespace query | plausible namespace list, no obvious unrelated namespace |
| `POD-B05` | source plausibility | first-run row count and reason mix are plausible | counts by reason and day/hour | no collapse or obviously missing dominant reasons |

## 3.3 Group C: Lifecycle Derivation Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `POD-C01` | lifecycle existence | touched pods produce lifecycle rows | distinct pods vs lifecycle count | lifecycle count is plausible for pods with importable events |
| `POD-C02` | timestamp derivation | `scheduled_at`, `first_started_at`, `last_failed_scheduling_at` match raw events | raw event sample vs lifecycle row | sampled rows match exactly |
| `POD-C03` | count derivation | `failed_scheduling_count` equals raw event count | aggregate sample query | sampled pods match |
| `POD-C04` | derived duration | `schedule_to_started_seconds` equals timestamp delta | SQL recomputation on samples | zero unexplained mismatch in samples |
| `POD-C05` | rerun stability | rerunning does not inflate lifecycle row count incorrectly | before/after lifecycle count | stable count except legitimate updates |

## 3.4 Group D: Jenkins Pod-Name Parsing Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `POD-D01` | parsing coverage | recent Jenkins pod rows produce `jenkins_build_url_key` at an acceptable rate | coverage query on Jenkins pod scope | coverage is measured and acceptable |
| `POD-D02` | parsing correctness | sampled `jenkins_build_url_key` values match the expected build URL path derived from the build row | sample pod rows vs build rows | sampled rows match exactly |
| `POD-D03` | ambiguity control | one parsed Jenkins key does not ambiguously resolve to multiple build rows without explanation | duplicate scan on parsed key to build mapping | zero unexplained ambiguities |
| `POD-D04` | one-to-many preservation | sampled multi-pod Jenkins builds remain as multiple lifecycle rows under the same build key | grouped sample query | fan-out is preserved and expected |

## 3.5 Group E: Build Linkage Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `POD-E01` | linkage coverage | lifecycle rows link back to builds at acceptable rate across both linkage paths | left join coverage query by build system | coverage is measured and acceptable |
| `POD-E02` | linkage correctness | sampled lifecycle rows point to the expected build metadata | sample join to `ci_l1_builds` using direct or metadata-derived linkage | sampled repo/job/cloud/build key are correct |
| `POD-E03` | collision risk | parsed Jenkins keys do not ambiguously join to multiple recent builds in a problematic way | duplicate scan on `jenkins_build_url_key` and matched build ids | collision rate is zero or explicitly explained |

## 3.6 Group F: Operational Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `POD-F01` | watermark behavior | successful run advances the relevant watermark/state | compare job state before/after | watermark moves forward coherently |
| `POD-F02` | failure safety | failed run preserves prior usable watermark | forced or observed failure review | prior watermark remains intact |
| `POD-F03` | idempotency | second run over overlapping window does not create duplicate raw rows | before/after row counts + duplicate check | stable unique-row count |
| `POD-F04` | runtime envelope | job completes within acceptable runtime/resource envelope | Job status, runtime, pod usage | runtime is acceptable for 10-15 minute scheduling |

## 4. Execution Phases

## Phase 0: Baseline Snapshot

Goal:
- capture the first stable post-smoke baseline before any rerun

Steps:
1. record `SHOW CREATE TABLE` for the pod tables
2. record row counts for `ci_l1_pod_events` and `ci_l1_pod_lifecycle`
3. record `ci_job_state` for `ci-sync-pods`
4. record max `event_timestamp` and max `receive_timestamp`

## Phase 1: Deterministic Structural Checks

Run first:
- `POD-A01`
- `POD-A02`
- `POD-A03`
- `POD-B02`
- `POD-B03`
- `POD-B04`

Why:
- quick checks
- catches schema and import-scope mistakes early

## Phase 2: Sample-Based Data Logic Checks

Run second:
- `POD-B01`
- `POD-B05`
- `POD-C01`
- `POD-C02`
- `POD-C03`
- `POD-C04`
- `POD-D01`
- `POD-D02`
- `POD-D03`
- `POD-D04`
- `POD-E01`
- `POD-E02`
- `POD-E03`

Sample design:
- at least 10 sampled pods
- include both normal startup and `FailedScheduling` examples if available
- include multiple jobs and at least one GCP-heavy namespace sample

## Phase 3: Operational Safety Checks

Run third:
- `POD-F01`
- `POD-F04`

Goal:
- decide whether recurring enablement is safe

## Phase 4: Idempotency Rerun Check

Run last and only after earlier phases pass:
- `POD-C05`
- `POD-F03`

Procedure:
- rerun the job with overlapping window
- compare counts and duplicate checks before/after

## 5. Suggested Validation Queries

These query shapes are intentionally simple and reproducible.

## 5.1 Freshness

```sql
SELECT
  MAX(event_timestamp) AS max_event_ts,
  MAX(receive_timestamp) AS max_receive_ts
FROM ci_l1_pod_events;
```

## 5.2 Duplicate Source Event Check

```sql
SELECT
  source_project,
  source_insert_id,
  COUNT(*) AS row_count
FROM ci_l1_pod_events
GROUP BY source_project, source_insert_id
HAVING COUNT(*) > 1;
```

## 5.3 Reason Mix

```sql
SELECT
  event_reason,
  COUNT(*) AS row_count
FROM ci_l1_pod_events
GROUP BY event_reason
ORDER BY row_count DESC;
```

## 5.4 Lifecycle Coverage

```sql
SELECT
  COUNT(*) AS lifecycle_rows,
  COUNT(DISTINCT pod_name) AS distinct_pod_names
FROM ci_l1_pod_lifecycle;
```

## 5.5 Jenkins Parsing Coverage

```sql
SELECT
  COUNT(*) AS jenkins_pod_rows,
  SUM(CASE WHEN jenkins_build_url_key IS NOT NULL THEN 1 ELSE 0 END) AS parsed_rows,
  ROUND(SUM(CASE WHEN jenkins_build_url_key IS NOT NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS parse_pct
FROM ci_l1_pod_lifecycle
WHERE build_system = 'JENKINS';
```

## 5.6 Build Linkage Coverage

```sql
SELECT
  build_system,
  COUNT(*) AS lifecycle_rows,
  SUM(CASE WHEN source_prow_job_id IS NOT NULL THEN 1 ELSE 0 END) AS linked_rows,
  ROUND(SUM(CASE WHEN source_prow_job_id IS NOT NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS link_pct
FROM ci_l1_pod_lifecycle l
GROUP BY build_system;
```

## 5.7 Collision Scan On Build Side

```sql
SELECT
  jenkins_build_url_key,
  COUNT(DISTINCT source_prow_job_id) AS build_count
FROM ci_l1_pod_lifecycle
WHERE build_system = 'JENKINS'
  AND jenkins_build_url_key IS NOT NULL
GROUP BY jenkins_build_url_key
HAVING COUNT(DISTINCT source_prow_job_id) > 1
ORDER BY build_count DESC, jenkins_build_url_key
LIMIT 50;
```

## 6. Expected Validation Output

The final validation report should include:

- a short executive summary
- a table of pass/fail by case id
- sampled mismatches with raw evidence
- lag and runtime observations
- explicit launch recommendation:
  - safe to enable recurring sync
  - safe with caveats
  - not safe yet

## 7. Exit Criteria For Recurring Enablement

We can enable recurring sync when:

1. schema checks pass
2. raw event uniqueness checks pass
3. lifecycle derivation samples pass
4. linkage coverage is acceptable and understood
5. rerun/idempotency checks pass
6. runtime is acceptable for the planned 10-15 minute scheduling
