# CI Dashboard V3 Implementation Plan

Status: Draft v0.1

Last updated: 2026-04-24

Related docs:
- `/Users/dillon/workspace/ee-apps-worktrees/v3-codex/ci-dashboard/docs/ci-dashboard-v3-jenkins-design.md`
- `/Users/dillon/workspace/ee-apps-worktrees/v3-codex/ci-dashboard/docs/ci-dashboard-v2-implementation.md`

## 1. Implementation Principles

- keep existing V1 and V2 page queries stable
- ship V3 in small, testable slices
- make Jenkins-first partial rows legal at the schema level
- make every new job idempotent or safely rerunnable
- prefer direct, boring implementations over extra abstraction in the first slice
- keep classification data collection ahead of dashboard work

## 2. Delivery Order

The recommended implementation order is:

1. schema and canonical merge foundation
2. Jenkins event ingest worker
3. redacted log archival job
4. AI classification and human revise path
5. rollout, sampling, and bounded backfill

### 2.1 Rollout Gates And Compatibility

The rollout order is not only an implementation preference; it is also a
compatibility requirement.

Safe gates:

1. schema migration only
2. schema migration + upgraded `sync-builds`
3. schema migration + upgraded `sync-builds` + Jenkins event ingest
4. schema migration + upgraded `sync-builds` + Jenkins event ingest + log
   archival
5. full rollout including AI classification

Compatibility notes:

- current V1 and V2 pages can keep running at every safe gate above
- `sync-pods`, `sync-pr-events`, `sync-flaky-issues`, and
  `refresh-build-derived` do not need to wait for later V3 phases
- the only unsafe rollout is enabling Jenkins ingest before the upgraded
  `sync-builds` merge path
- if rollout pauses at any safe gate, current production V1 or V2 behavior
  remains available

## 3. Phase A: Schema And Canonical Merge Foundation

### 3.1 Deliverables

1. `sql/014_alter_ci_l1_builds_for_v3_jenkins.sql`
2. `sql/015_create_ci_l1_jenkins_build_events.sql`
3. Jenkins-first nullable support in build-row model and sync logic
4. canonical-key-first merge path in `sync_builds.py`
5. prerequisite duplicate-audit queries for `normalized_build_url`

### 3.2 Schema Scope

Migration `014_alter_ci_l1_builds_for_v3_jenkins.sql` should:

- relax existing columns that block Jenkins-first rows:
  - `source_prow_row_id`
  - `source_prow_job_id`
  - `namespace`
  - `job_name`
  - `job_type`
  - `org`
  - `repo`
  - `repo_full_name`
  - `start_time`
- add V3 Jenkins evidence fields:
  - `source_jenkins_event_id`
  - `source_jenkins_job_url`
  - `source_jenkins_result`
  - `build_params_json`
  - `log_gcs_uri`
  - `log_archived_at`
  - `ai_error_l1_category`
  - `ai_error_l2_subcategory`
  - `ai_classification_source`
  - `ai_classification_confidence`
  - `ai_classified_at`
  - `ai_provider_name`
  - `ai_model_name`
  - `ai_evidence_text`
  - `human_error_l1_category`
  - `human_error_l2_subcategory`
  - `human_reviewed_at`
  - `human_reviewer`

Migration `015_create_ci_l1_jenkins_build_events.sql` should:

- create the raw Jenkins event audit table
- enforce uniqueness on `event_id`
- index `normalized_build_url`

### 3.3 Code Changes

Expected file updates:

- `src/ci_dashboard/common/models.py`
  - make `source_prow_row_id` optional
  - make `source_prow_job_id` optional
- `src/ci_dashboard/jobs/sync_builds.py`
  - stop assuming Prow ids always exist on canonical rows
  - add canonical-key-first lookup and merge path
  - keep historical insert behavior as fallback when no `normalized_build_url` match exists
- `src/ci_dashboard/common/config.py`
  - no V3 settings required yet in Phase A unless migration tooling needs them

### 3.4 Exit Criteria

- migrations run on sqlite and TiDB target
- Jenkins-first rows are legal in `ci_l1_builds`
- `sync-builds` can merge into a Jenkins-created row by `normalized_build_url`
- duplicate-key audit for `normalized_build_url` has been run and documented

## 4. Phase B: Jenkins Event Ingest

### 4.1 Deliverables

1. Dedicated Kafka topic `jenkins-event`
2. `cloudevents-server` producer `topic_mapping` entry for Jenkins finished events
3. Jenkins JCasC sink URL update to `/events`
4. `src/ci_dashboard/jobs/jenkins_worker.py`
5. CLI wiring for the worker command
6. event-to-build mapping logic
7. replay-safe audit writes to `ci_l1_jenkins_build_events`

### 4.2 Worker Responsibilities

The worker should:

- consume `jenkins-event`
- parse CloudEvent payload
- compute `normalized_build_url`
- map Jenkins result into existing `state`
- create or enrich a canonical build row
- persist raw event audit data
- commit Kafka offset only after DB write succeeds

Bridge notes:

- Jenkins runs a patched CD Events plugin build that emits structured CloudEvents
  JSON over HTTP
- Jenkins JCasC points `httpSinkUrl` to the existing `POST /events` endpoint
- `cloudevents-server` producer `topic_mapping` routes
  `dev.cdevents.pipelinerun.finished.0.1.0` to Kafka topic `jenkins-event`
- other Jenkins `dev.cdevents.*` event types are ignored before they reach Kafka
- existing non-Jenkins `POST /events` routing and default topic remain unchanged

### 4.3 Jenkins-First Fill Rules

The worker should use the design doc fill strategy directly:

- `url` from Jenkins build URL
- `state` from mapped Jenkins result
- `job_name` from normalized pipeline name when available
- `start_time` from event timing when available, otherwise `NULL`
- `org`, `repo`, `repo_full_name` from allowlisted params when available
- missing Prow-side columns remain `NULL`

### 4.4 CLI Shape

Recommended new CLI command:

```bash
ci-dashboard consume-jenkins-events
```

Possible future variants:

```bash
ci-dashboard consume-jenkins-events --max-messages 100
ci-dashboard consume-jenkins-events --group-id ci-dashboard-v3-jenkins-worker
```

### 4.5 Exit Criteria

- at least one real Jenkins finished event consumed successfully
- event audit row created exactly once per `event_id`
- canonical build row created or enriched correctly
- duplicate delivery does not create duplicate audit rows

## 5. Phase C: Redacted Log Archival

### 5.1 Deliverables

1. `src/ci_dashboard/jobs/archive_error_logs.py`
2. `src/ci_dashboard/jobs/jenkins_client.py`
3. `src/ci_dashboard/jobs/gcs_client.py`
4. CLI wiring for archive job
5. in-memory log redaction helper inside `archive_error_logs.py`

### 5.2 Archive Job Responsibilities

The archive job should:

- scan for terminal non-success Jenkins builds with `log_gcs_uri IS NULL`
- fetch bounded tail via `logText/progressiveText`
- retain only bounded tail bytes
- sanitize text in memory
- upload one redacted artifact to GCS
- update `log_gcs_uri` and `log_archived_at`

### 5.3 Concurrency And Retry Rules

- first slice runs serially so Jenkins load stays predictable
- scheduled runs skip rows that already have `log_gcs_uri`
- manual rerun may overwrite the same object path
- fetch or upload failure leaves `log_gcs_uri` as `NULL`

### 5.4 CLI Shape

Recommended commands:

```bash
ci-dashboard archive-error-logs
ci-dashboard archive-error-logs --limit 50
ci-dashboard archive-error-logs --build-id 12345 --force
```

### 5.5 Exit Criteria

- sampled error builds archive a redacted tail artifact successfully
- overwrite rerun refreshes the same GCS object path
- no raw log text is durably stored in TiDB or GCS

## 6. Phase D: AI Classification And Human Revise

### 6.1 Deliverables

1. `src/ci_dashboard/jobs/analyze_errors.py`
2. `src/ci_dashboard/jobs/llm_classifier.py`
3. `src/ci_dashboard/jobs/rule_engine.py`
4. taxonomy file `error_taxonomy.yaml`
5. CLI path for human revise

### 6.2 Classification Responsibilities

The analysis job should:

- read rows with `log_gcs_uri IS NOT NULL`
- skip rows with `human_reviewed_at IS NOT NULL`
- refresh AI fields only when missing or stale relative to `log_archived_at`
- run deterministic rules first
- call LLM only on rule miss
- write AI results directly back onto `ci_l1_builds`

### 6.3 Human Revise Responsibilities

The first slice should not build a review UI.

Minimum viable path:

- one CLI command or internal admin path that updates:
  - `human_error_l1_category`
  - `human_error_l2_subcategory`
  - `human_reviewed_at`
  - `human_reviewer`

### 6.4 CLI Shape

Recommended commands:

```bash
ci-dashboard analyze-errors
ci-dashboard analyze-errors --limit 100
ci-dashboard review-error --build-id 12345 --l1 INFRA --l2 NETWORK --reviewer dillon
```

### 6.5 Exit Criteria

- AI fields populate on sampled rows
- AI result stores provider, model, confidence, and evidence text
- human revise can be written independently
- effective query logic works as `COALESCE(human, ai)`

## 7. Phase E: Rollout, Validation, And Backfill

### 7.1 Deliverables

1. bounded production rollout plan
2. sampling checklist for merge, archive, and classification quality
3. `backfill-jenkins` CLI path
4. post-rollout validation report

### 7.2 Backfill Responsibilities

Backfill should:

- reuse canonical merge logic
- respect the same archive overwrite rules
- respect the same AI-refresh and human-review skip rules
- avoid touching Kafka offsets

### 7.3 CLI Shape

Recommended command:

```bash
ci-dashboard backfill-jenkins --start-date 2026-03-24 --end-date 2026-04-24
```

### 7.4 Exit Criteria

- production freshness is healthy
- sampled builds reconcile across Jenkins event, canonical build row, and archived log
- human-reviewed rows remain stable after recurring analysis runs

## 8. File-Level Change Plan

### 8.1 SQL

- add `sql/014_alter_ci_l1_builds_for_v3_jenkins.sql`
- add `sql/015_create_ci_l1_jenkins_build_events.sql`

### 8.2 Common Models And Config

- update `src/ci_dashboard/common/models.py`
- update `src/ci_dashboard/common/config.py`

Expected new settings:

- Kafka brokers and topic
- Jenkins base URL and optional auth
- GCS bucket
- log-tail byte cap
- Jenkins fetch concurrency
- LLM provider, model, and secret-backed API key

### 8.3 Jobs

Expected new modules:

- `src/ci_dashboard/jobs/jenkins_worker.py`
- `src/ci_dashboard/jobs/archive_error_logs.py`
- `src/ci_dashboard/jobs/analyze_errors.py`
- `src/ci_dashboard/jobs/llm_classifier.py`
- `src/ci_dashboard/jobs/rule_engine.py`
- `src/ci_dashboard/jobs/jenkins_client.py`
- `src/ci_dashboard/jobs/gcs_client.py`

Expected existing-job changes:

- `src/ci_dashboard/jobs/sync_builds.py`
- `src/ci_dashboard/jobs/cli.py`

## 9. Test Plan

### 9.1 Unit Tests

- Jenkins result -> `state` mapping
- `normalized_build_url` merge behavior
- Jenkins-first partial-row insert behavior
- duplicate event dedup by `event_id`
- tail truncation and in-memory redaction
- rule-engine first, LLM second behavior
- human revise precedence over AI fields

### 9.2 Integration Or Smoke

- run migrations on sqlite and TiDB target
- run `sync-builds` with canonical-key-first merge enabled
- run one sampled Jenkins event through worker -> archive -> analyze
- verify rerun overwrite behavior for archived logs
- verify reanalysis skips `human_reviewed_at IS NOT NULL`

### 9.3 Regression

- V1 and V2 jobs still run
- existing `CI Status` page payload remains stable
- existing flaky-signal refresh path remains stable

## 10. Immediate Next Coding Order

The recommended immediate coding order is:

1. write `014_alter_ci_l1_builds_for_v3_jenkins.sql`
2. write `015_create_ci_l1_jenkins_build_events.sql`
3. update `NormalizedBuildRow` optional fields
4. refactor `sync_builds.py` to merge by `normalized_build_url` first
5. only then start `jenkins_worker.py`

This keeps Phase 1 unblocked and gives the later jobs a stable canonical-build
foundation to build on.
