# CI Dashboard V3 Jenkins Data Collection Design

Status: Draft v0.6

Last updated: 2026-04-24

Reference inputs:
- `ci-dashboard/docs/ci-dashboard-v1-design.md`
- `ci-dashboard/docs/ci-dashboard-v2-design.md`
- `ci-dashboard/docs/ci-dashboard-v2-implementation.md`
- `ci-dashboard/src/ci_dashboard/jobs/sync_builds.py`
- `ci-dashboard/src/ci_dashboard/jobs/sync_pods.py`
- `ci-dashboard/sql/001_create_ci_l1_builds.sql`
- `/Users/dillon/workspace/skills_index.md`
- `/Users/dillon/workspace/Skills-collection/local-skills/ci_job_debug.skills/SKILL.md`
- `cloudevents-server/`
- Jenkins CD Events Plugin public docs and source

## 1. Background

V1 established the current CI metrics baseline:

- `ci_l1_builds` as the primary build fact table
- `ci_l1_pr_events` as the normalized PR event table
- Prow-driven hourly synchronization as the main build ingest path
- FastAPI + dashboard pages for build and PR exploration

V2 extended observability into Kubernetes execution evidence:

- pod lifecycle collection
- Jenkins pod linkage through normalized Jenkins URL semantics
- decomposition of queue, scheduling, startup, and execution time

V3 addresses the next missing layer:

1. **Jenkins-native completion evidence**
- current V1 or V2 data mostly comes from `prow_jobs`
- we still do not systematically collect:
  - Jenkins completion events
  - selected build parameters
  - console-log evidence

2. **Operationally useful failure attribution**
- current failure fields are intentionally conservative
- they are not designed for:
  - AI-assisted classification
  - human correction
  - side-by-side comparison between AI and human judgment
  - long-term insight into infra improvement opportunities

The first V3 slice is a data-foundation project, not a dashboard publish
project.

## 2. Objectives

- collect Jenkins events in near real time through CD Events Plugin ->
  Jenkins-compatible HTTP sink -> existing `cloudevents-server` -> Kafka
- allow Jenkins finished events to create or enrich canonical build rows before
  `sync-builds` catches up
- archive one bounded **redacted** console-log tail per terminal non-success
  Jenkins build
- support overwrite rerun of log archival
- classify failures through deterministic rules plus pluggable LLM fallback
- store one AI judgment plus one optional human revise per build
- persist enough structured evidence for later API and dashboard work
- support replay and bounded historical backfill

## 3. Explicit Non-Goals For The First V3 Slice

- no dashboard UI delivery in this phase
- no requirement to ingest Jenkins start events
- no requirement to preserve stage-level or TaskRun-level semantics in the V3
  worker
- no requirement to store every full console log
- no requirement to persist raw console-log artifacts
- no requirement to persist multi-step classification history
- no replacement of existing V1/V2 pod evidence paths
- no destructive migration that breaks current V1/V2 queries during rollout

## 3.1 Compatibility And Upgrade Dependencies

### Compatibility Guarantees

V3 is designed to be forward-compatible with the currently running V1 and V2
flows.

Schema compatibility:

- V3 does not remove existing V1 or V2 tables
- V3 does not rename existing V1 or V2 columns
- V3 mostly adds new columns or relaxes existing `NOT NULL` constraints to allow
  Jenkins-first partial rows
- existing `failure_category` and `failure_subcategory` semantics stay unchanged

Job compatibility:

- `sync-pr-events` continues unchanged
- `sync-flaky-issues` continues unchanged
- `refresh-build-derived` continues writing the existing flaky-related fields
- `sync-pods` remains a V2 path and has no direct runtime dependency on V3 jobs
- `sync-builds` is the only existing job that must be upgraded for V3

API and dashboard compatibility:

- current V1 or V2 APIs do not need the new V3 columns
- current dashboard pages continue using the existing V1 or V2 query paths
- V3 data collection can go live before any V3 dashboard work starts

What current V1 and V2 will actually observe:

- after schema migration, existing V1 and V2 reads keep working because their
  referenced columns still exist and keep the same meaning
- V1 pages and APIs do not need to be redeployed together with V3
- V2 pod evidence collection does not wait for Jenkins event ingest, log
  archival, or AI classification
- if V3 jobs are disabled, the system still behaves like today's V1 or V2
  system
- once V3 jobs are enabled, they only enrich the same canonical build rows; they
  do not require current pages to read new fields immediately

What can affect current behavior:

- schema migration is low risk because it is additive or relaxes constraints, not
  destructive
- the only existing runtime path that changes behavior is `sync-builds`
- `sync-builds` changes are required so that Jenkins-first rows do not later
  become duplicate rows
- no other existing scheduled job needs to change semantics for the first V3
  slice

### Upgrade Dependencies

V3 rollout has a strict dependency order.

Hard dependencies:

1. apply schema migrations first
2. upgrade `sync-builds` to merge by `normalized_build_url` before or together
   with enabling Jenkins event ingest
3. enable Jenkins event ingest only after the canonical-merge path is live
4. enable log archival only after Jenkins event ingest is creating or enriching
   build rows
5. enable AI classification only after redacted log archival is writing
   `log_gcs_uri`

Safe partial rollout states:

- schema only:
  - V1 and V2 continue normally
  - V3 features remain inactive
- schema + upgraded `sync-builds`:
  - V1 and V2 continue normally
  - canonical merge foundation is ready
- schema + upgraded `sync-builds` + Jenkins worker:
  - V3 event collection starts
  - archive and AI can still remain disabled

Unsafe rollout state:

- enabling Jenkins event ingest before the upgraded `sync-builds` rollout:
  - risks duplicate canonical rows or merge failure later

### Dependency Matrix

| Capability | Depends On | Impact On Existing V1/V2 |
|---|---|---|
| schema migration | none | safe, additive, existing reads keep working |
| upgraded `sync-builds` | schema migration | required for V3, should be deployed before Jenkins ingest |
| Jenkins event ingest | schema migration, upgraded `sync-builds` | no direct V1/V2 query dependency, but unsafe without upgraded `sync-builds` |
| log archival | Jenkins event ingest | no impact on V1/V2 if disabled |
| AI classification | log archival | no impact on V1/V2 if disabled |
| future V3 dashboard | collected V3 data | fully decoupled from current V1/V2 pages |

Operational interpretation:

- V1 and V2 do not depend on log archival or AI classification
- V3 does depend on the `sync-builds` merge change
- this means rollout can pause after schema or after upgraded `sync-builds`
  without breaking current production usage
- if needed, rollback is operationally simple:
  - disable Jenkins ingest, archive, and AI jobs
  - keep schema in place
  - let V1 and V2 continue on the existing query paths

## 4. Key Design Decisions

### 4.1 Canonical Build Table Remains `ci_l1_builds`

`ci_l1_builds` remains the canonical build table, but its identity model
changes.

V1/V2 implicit identity:

- one row per `source_prow_job_id`

V3 identity:

- one canonical build row merged from multiple evidence sources
- Jenkins finished events may create the row first
- later `sync-builds` from `prow_jobs` must merge into that same row

### 4.2 `normalized_build_url` Becomes The Canonical Merge Key

V3 does not introduce a second canonical-key concept. Instead, it formally
promotes the existing `normalized_build_url` to the canonical cross-source merge
key for Jenkins-backed builds.

Why this is the preferred choice:

- V1 already computes `normalized_build_url` from Jenkins URLs
- V2 pod linkage already depends on the same Jenkins URL normalization semantics
- `sync_builds.py` already emits `normalized_build_url` for `ci_l1_builds`
- reusing the existing key reduces migration risk and avoids parallel identity
  systems

Schema implication:

- `source_prow_row_id` must become nullable
- `source_prow_job_id` must become nullable
- uniqueness must eventually be enforced on `normalized_build_url` after data
  cleanup
- `sync-builds` must stop treating `source_prow_job_id` as the only upsert key

### 4.3 Durable Async Handoffs Between Stages

V3 intentionally separates:

1. event ingest
2. canonical build merge
3. log archival
4. error analysis
5. human correction

Reason:

- Kafka ingest should not block on Jenkins REST latency or GCS latency
- retries should be stage-local and observable
- replay of one stage should not require replay of all downstream side effects

### 4.4 Only One Effective Redacted Log Artifact Per Build

The first V3 slice keeps archive design intentionally simple:

- one error build maps to one effective redacted tail artifact
- archive rerun overwrites the same effective object path
- no separate artifact-history table in the first slice
- no archive state machine beyond presence or absence of the effective fields

This keeps schema and operator workflow lightweight while still supporting
overwrite rerun.

### 4.5 Mandatory Redaction Boundary

V3 must not send raw Jenkins logs or unfiltered build parameters directly to an
external model.

Required policy:

- raw console text may exist only in memory during fetch
- Jenkins tail must be sanitized before any durable storage in GCS or TiDB
- LLM input must use sanitized log text only
- build parameters stored in TiDB must be allowlisted, not copied wholesale

### 4.6 Two-Slot Classification Model

The first V3 slice does not keep full classification history.

Instead, each build stores:

- one machine judgment
- one optional human revise

Why this is enough for the first slice:

- it preserves the latest AI output
- it preserves the latest human correction
- it allows direct comparison between AI and human labels
- it avoids a revision table, supersession logic, and effective-verdict
  projection machinery

Effective verdict rule:

- use human revise if present
- otherwise use AI judgment

### 4.7 Provider Is Pluggable

The LLM classifier should support multiple providers through one interface.

Initial supported provider family:

- OpenAI
- Gemini
- Qianwen

Design implication:

- provider choice is runtime configuration, not first-slice schema identity

## 5. High-Level Architecture

### 5.1 Data Flow

```text
Jenkins Controller
  |
  |  CD Events Plugin (HTTP sink)
  |  structured CloudEvents JSON, POST /events
  v
cloudevents-server (existing)
  POST /events
  -> topic_mapping routes dev.cdevents.pipelinerun.finished.0.1.0 to jenkins-event
  -> ignore other dev.cdevents.* event types
  v
Kafka topic: jenkins-event
  v
ci-dashboard Jenkins Event Worker
  1. validate/parse CloudEvent
  2. persist raw Jenkins event audit row
  3. create or enrich canonical ci_l1_builds row

archive-error-logs job
  1. scan terminal non-success Jenkins builds without log_gcs_uri
  2. fetch bounded tail through Jenkins progressive text API
  3. keep bounded tail in memory
  4. sanitize in memory
  5. upload one redacted artifact to GCS
  6. update log_gcs_uri on ci_l1_builds

analyze-errors job
  1. scan builds where log_gcs_uri is present
  2. refresh AI judgment when needed
  3. rule engine first
  4. LLM fallback on sanitized input only
  5. update AI classification fields on ci_l1_builds

human review path
  1. inspect AI judgment + evidence
  2. optionally fill human revise fields
  3. dashboard and queries use human revise first when present
```

### 5.2 Component Inventory

| Component | Language | New or Existing | Description |
|---|---|---|---|
| `cloudevents-server` | Go | Existing, small code change | route Jenkins finished events from `/events` into Kafka topic `jenkins-event` and ignore other Jenkins event types |
| Kafka topic `jenkins-event` | - | New | Jenkins event topic |
| Jenkins Event Worker | Python | New | event audit + canonical build merge |
| Error Log Archive Job | Python | New | Jenkins fetch -> in-memory redact -> one GCS artifact |
| Error Analysis Job | Python | New | rule engine + pluggable LLM -> AI fields on build row |
| taxonomy config | YAML | New | versioned categories and deterministic rules |

## 6. External Assumptions And Bridge Strategy

### 6.1 Jenkins Event Shape

Real-cluster validation and local reproduction showed that the upstream Jenkins
CD Events plugin needed one small fix on our side:

- the stock plugin posts directly to the configured `httpSinkUrl`
- the stock plugin implementation did not emit standard structured CloudEvents
  JSON
- emits more than one event family, including `taskrun.started` and
  `taskrun.finished`

### 6.2 Official Ingress Path

V3 therefore defines the official ingress path as:

- Jenkins CD Events Plugin -> HTTP sink
- patched Jenkins CD Events plugin -> `cloudevents-server` `POST /events`
- `cloudevents-server` -> Kafka

This is preferred because:

- the repo already contains `cloudevents-server`
- the runtime change in `cloudevents-server` stays small and isolated in one
  routing layer
- it avoids building a separate Jenkins-only bridge first
- it reuses the existing CloudEvents ingress path and topic-mapping model

### 6.3 Required `cloudevents-server` Config

```yaml
jenkins:
  http_sink_url: http://cloudevents-server.cs.svc/events

kafka:
  producer:
    topic_mapping:
      dev.cdevents.pipelinerun.finished.0.1.0: jenkins-event
```

V3 requires a small routing change in `cloudevents-server`:

- `dev.cdevents.pipelinerun.finished.0.1.0` must map to Kafka topic
  `jenkins-event`
- other Jenkins `dev.cdevents.*` event types should be ignored before Kafka
- the existing global Kafka `default_topic` remains unchanged for non-Jenkins
  traffic

### 6.4 Kafka Message Key Implications

Current bridge behavior uses CloudEvent `id` as the Kafka message key.

Implications:

- event dedup must remain based on `event_id`
- canonical build dedup must happen in the worker and database layer
- V3 must not assume Kafka ordering by build identity

## 7. Canonical Build Identity And Merge Semantics

### 7.1 Canonical Merge Key

For Jenkins-backed builds, the canonical merge key is:

- `normalized_build_url`

Source mapping:

- Jenkins finished event derives it from normalized Jenkins build URL
- `sync-builds` derives it from `prow_jobs.url`
- V2 pod linkage continues using the same normalization family

### 7.2 No Persisted Source-State Columns

The first V3 slice does not persist source-state columns such as
`build_record_state` or `first_seen_source`.

Reason:

- final consistency is what matters for later analysis
- merge completeness can be inferred from whether Jenkins-side and Prow-side
  source ids are both present
- removing derivable source-state fields reduces drift risk

### 7.3 Jenkins Event Worker Merge Rules

When the Jenkins Event Worker processes a finished event:

1. compute `normalized_build_url`
2. upsert raw event audit table by `event_id`
3. look up `ci_l1_builds` by `normalized_build_url`
4. if found:
- enrich the existing canonical row
5. if not found:
- create a new canonical row with:
  - `source_prow_row_id = NULL`
  - `source_prow_job_id = NULL`
  - `source_jenkins_event_id` populated
  - existing timing fields filled from Jenkins event when available

### 7.4 `sync-builds` Merge Rules

When `sync-builds` later processes the matching `prow_jobs` row:

1. compute the same `normalized_build_url` from `url`
2. first try match by `normalized_build_url`
3. if found:
- enrich the existing row
- set `source_prow_row_id`
- set `source_prow_job_id`
- preserve existing timing fields unless the implementation explicitly decides a
  missing field should be backfilled
4. if not found:
- fall back to the historical insert path
- create a new row

### 7.5 Compatibility Requirement For `sync-builds`

This is the most important V3 code-path change.

`sync-builds` cannot remain:

- `INSERT ... ON CONFLICT(source_prow_job_id) DO UPDATE`

It must become:

- canonical-key-aware merge first
- Prow-id-based insert or update second

Without this change, Jenkins-first canonical rows will duplicate or fail to
merge later.

## 8. Proposed Schema Changes

The SQL below is design-level and may later be split into multiple migrations.

### 8.1 Alter `ci_l1_builds`

```sql
-- illustrative design-level migration, not final syntax

ALTER TABLE ci_l1_builds
  MODIFY COLUMN source_prow_row_id BIGINT NULL,
  MODIFY COLUMN source_prow_job_id CHAR(36) NULL,
  MODIFY COLUMN namespace VARCHAR(255) NULL,
  MODIFY COLUMN job_name VARCHAR(255) NULL,
  MODIFY COLUMN job_type VARCHAR(32) NULL,
  MODIFY COLUMN org VARCHAR(63) NULL,
  MODIFY COLUMN repo VARCHAR(63) NULL,
  MODIFY COLUMN repo_full_name VARCHAR(127) NULL,
  MODIFY COLUMN start_time DATETIME NULL,
  ADD COLUMN source_jenkins_event_id VARCHAR(128) NULL AFTER build_system,
  ADD COLUMN source_jenkins_job_url VARCHAR(1024) NULL AFTER source_jenkins_event_id,
  ADD COLUMN source_jenkins_result VARCHAR(32) NULL AFTER source_jenkins_job_url,
  ADD COLUMN build_params_json JSON NULL AFTER source_jenkins_result,
  ADD COLUMN log_gcs_uri VARCHAR(512) NULL AFTER build_params_json,
  ADD COLUMN error_l1_category VARCHAR(32) NULL AFTER log_gcs_uri,
  ADD COLUMN error_l2_subcategory VARCHAR(64) NULL AFTER error_l1_category,
  ADD COLUMN revise_error_l1_category VARCHAR(32) NULL AFTER error_l2_subcategory,
  ADD COLUMN revise_error_l2_subcategory VARCHAR(64) NULL AFTER revise_error_l1_category;
```

#### 8.1.1 Jenkins-First Column Fill Strategy

For Jenkins-first rows, the worker must fill or relax every existing required
column that cannot be guaranteed by the CDEvent payload.

| Column | Strategy For Jenkins-First Row |
|---|---|
| `source_prow_row_id` | `NULL` until `sync-builds` merges |
| `source_prow_job_id` | `NULL` until `sync-builds` merges |
| `namespace` | `NULL` in the first slice |
| `job_name` | normalize from Jenkins pipeline name when available, otherwise `NULL` and backfill later |
| `job_type` | `NULL` in the first slice |
| `state` | mapped from Jenkins result into existing build-state vocabulary |
| `org` | parse from allowlisted build params when present, otherwise `NULL` |
| `repo` | parse from allowlisted build params when present, otherwise `NULL` |
| `repo_full_name` | derive from `org` and `repo` when both are known, otherwise `NULL` |
| `url` | Jenkins build URL from event payload |
| `start_time` | use Jenkins-reported start time when present; otherwise allow `NULL` and backfill later |

Other existing fields continue using current behavior:

- `cloud_phase` stays driven by URL classification or existing default
- `build_system` stays driven by URL classification
- `optional`, `report`, and `is_pr_build` continue using existing defaults
- Jenkins-first rows are allowed to be temporarily partial as long as later merge
  or backfill can complete them

#### 8.1.2 Jenkins Result Versus Existing `state`

- `source_jenkins_result` preserves the original Jenkins result string for audit
- `state` continues using the existing build-state vocabulary

Recommended mapping:

- `SUCCESS` -> `success`
- `FAILURE` -> `failure`
- `UNSTABLE` -> `failure`
- `ABORTED` -> `aborted`
- `NOT_BUILT` -> `error`

Required follow-up:

- backfill and validate `normalized_build_url` quality for existing rows
- find and clean duplicate non-null `normalized_build_url` values before
  enforcing uniqueness
- after cleanup, add:
  - `UNIQUE KEY uk_ci_l1_builds_normalized_build_url (normalized_build_url(768))`
- keep the unique property of `source_prow_job_id`
  - MySQL or TiDB unique-on-nullable semantics allow multiple `NULL` values

Prerequisite audit SQL:

```sql
SELECT normalized_build_url, COUNT(*) AS cnt
FROM ci_l1_builds
WHERE normalized_build_url IS NOT NULL
GROUP BY normalized_build_url
HAVING cnt > 1
ORDER BY cnt DESC
LIMIT 50;

SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN normalized_build_url IS NULL THEN 1 ELSE 0 END) AS null_count
FROM ci_l1_builds
WHERE build_system = 'JENKINS';
```

Impact checklist before implementation:

- `ci_dashboard/common/models.py`
  - `NormalizedBuildRow.source_prow_row_id` becomes optional
  - `NormalizedBuildRow.source_prow_job_id` becomes optional
- `ci_dashboard/jobs/sync_builds.py`
  - `map_build_row()` can no longer assume Jenkins-first rows already have Prow ids
  - `_build_upsert_statement()` must stop using only `source_prow_job_id` as the conflict identity
  - canonical-key-first merge path must be added
- SQLite test compatibility must be checked explicitly
  - do not assume nullable unique behavior is identical to TiDB or MySQL

### 8.2 Create `ci_l1_jenkins_build_events`

Purpose:

- raw Jenkins event audit
- replay safety
- event-level failure observability

```sql
CREATE TABLE IF NOT EXISTS ci_l1_jenkins_build_events (
  id BIGINT NOT NULL AUTO_INCREMENT,
  event_id VARCHAR(128) NOT NULL,
  event_type VARCHAR(128) NOT NULL,
  event_time DATETIME NULL,
  received_at DATETIME NOT NULL,
  normalized_build_url VARCHAR(1024) NULL,
  build_url VARCHAR(1024) NULL,
  result VARCHAR(32) NULL,
  payload_json JSON NOT NULL,
  processing_status VARCHAR(32) NOT NULL DEFAULT 'RECEIVED',
  last_error TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ci_l1_jenkins_build_events_event_id (event_id),
  KEY idx_ci_l1_jenkins_build_events_build_key (normalized_build_url(768))
);
```

### 8.3 Classification Lives On `ci_l1_builds`

The first V3 slice does not create a separate classification table.

Reason:

- one AI judgment plus one optional human revise is enough for the initial
  learning loop
- direct build-row storage is simpler than revision numbering, supersession, and
  projection logic

Effective-query pattern:

```sql
COALESCE(revise_error_l1_category, error_l1_category)
COALESCE(revise_error_l2_subcategory, error_l2_subcategory)
```

### 8.4 Relationship To Existing `failure_category` Fields

Existing V1 or V2 columns remain unchanged:

- `failure_category`
- `failure_subcategory`

First-slice rule:

- `refresh-build-derived` continues writing existing flaky-related values such as
  `FLAKY_TEST`
- V3 AI or human classification fields are a separate dimension
- no first-slice migration attempts to replace or reinterpret the old columns

Future dashboard work may later unify or cross-reference these dimensions, but
the first slice treats them as independent.

## 9. Secure Data Handling And Redaction

### 9.1 Build Parameters

Build parameters must not be copied into TiDB indiscriminately.

Required rule:

- persist only an allowlist of non-sensitive keys
- drop, hash, or mask everything else

Examples of fields that may be allowed:

- PR number
- target branch
- repo identifier
- explicitly non-secret feature flags

Examples of fields that should be excluded by default:

- tokens
- passwords
- cookie-like strings
- credentials
- internal URLs with embedded auth

### 9.2 Single Redacted Artifact Model

The first V3 slice persists only one redacted log tail artifact per error build.

Important consequences:

- raw console text is not persisted in GCS in the first slice
- sanitization happens in memory before durable storage
- rerun overwrites the effective GCS object at the same path

### 9.3 Initial Redaction Rule Set

| Pattern | Action | Example |
|---|---|---|
| token-like string after `token=`, `password=`, `Bearer ` | replace with `[REDACTED]` | `Bearer ghp_abc123` -> `Bearer [REDACTED]` |
| internal IPs such as `10.x.x.x` or `172.16-31.x.x` | replace with `[INTERNAL_IP]` | `10.128.15.234` -> `[INTERNAL_IP]` |
| email addresses | replace with `[EMAIL]` | `alice@example.com` -> `[EMAIL]` |
| internal URL query secret parameters | mask secret value only | `...?token=abc` -> `...?token=[REDACTED]` |
| home or Jenkins paths containing usernames | normalize user segment | `/home/alice/...` -> `/home/[USER]/...` |

Implementation note:

- in the first slice, this can live as a helper function inside
  `archive_error_logs.py`

### 9.4 LLM Boundary

External model input must use:

- redacted log text only
- allowlisted build metadata only

The design must not assume:

- raw console text can be sent to the provider safely
- provider retention is automatically acceptable for CI evidence

Provider policy and retention must be documented before production rollout.

### 9.5 Few-Shot Example Policy

V3 does not store raw production log snippets in git.

Allowed in repo:

- taxonomy definitions
- deterministic rule metadata
- synthetic examples
- manually sanitized examples

Not allowed in repo:

- raw production log fragments copied directly from Jenkins builds

## 10. Error Taxonomy And Classification

### 10.1 Initial Taxonomy

Recommended L1 categories:

| L1 | Description |
|---|---|
| `INFRA` | infrastructure or platform problem rather than product or test logic |
| `BUILD` | compilation, dependency, packaging, job-config, or build-stage failure |
| `UT` | unit-test failure |
| `IT` | integration-test or end-to-end test failure |
| `OTHERS` | cannot classify confidently |

Recommended initial INFRA L2 subcategories:

| L2 | Typical Patterns |
|---|---|
| `JENKINS` | agent lost, remoting failure, controller-side issue |
| `K8S` | scheduling timeout, PVC attach failure, OOM, eviction, node-pool issue |
| `NETWORK` | DNS, timeout, connection reset, service unreachable |
| `STORAGE` | disk full, quota exceeded, write failure |
| `EXTERNAL_DEP` | git mirror, artifact mirror, API rate limit, upstream service issue |

Recommended initial BUILD L2 subcategories:

| L2 | Typical Patterns |
|---|---|
| `COMPILE` | compiler error, syntax error |
| `DEPENDENCY` | dependency resolution, missing module |
| `PACKAGING` | image build, packaging, archive step |
| `PIPELINE_CONFIG` | Jenkinsfile, script wiring, missing env or config |

### 10.2 Classification Pipeline

```text
redacted log tail
  |
  v
rule engine (taxonomy file)
  |
  |-- match
  |   -> update error_l1_category / error_l2_subcategory
  |
  `-- no match
      -> LLM classification on sanitized input
      -> update error_l1_category / error_l2_subcategory

human review
  -> optionally update revise fields

effective query
  -> revise if present, otherwise machine judgment
```

### 10.3 Provider Strategy

Classifier interface should be pluggable, for example through an
`LLMClassifier` protocol or equivalent abstraction.

Provider is selected by runtime config and may be:

- OpenAI
- Gemini
- Qianwen

### 10.4 Effective Verdict Rule

Recommended precedence:

1. revise if present
2. otherwise machine judgment

### 10.5 Idempotency

- event ingest dedup:
  - `ci_l1_jenkins_build_events.event_id`
- canonical build dedup:
  - `normalized_build_url`
- archive dedup:
  - `log_gcs_uri` already exists unless explicit overwrite rerun is requested
- classification refresh:
  - machine classification updates only when missing or an explicit rerun is
    requested

## 11. Console Log Handling

### 11.1 Supported Access Path

Current environment may allow anonymous console access, but V3 should define a
supported server-side path as:

- internal Jenkins endpoint
- authenticated access when credentials are available

Initial deployment may use the current anonymous path if that is what the
internal Jenkins service exposes today, but the design keeps a clean upgrade
path to a read-only robot credential.

### 11.2 Truncation Strategy

The first V3 slice archives only a bounded tail window.

Recommended default policy:

- keep the latest `262144` bytes in a rolling buffer

Why byte-based tailing is preferred now:

- storage stays bounded
- implementation is simpler
- later review can still open the effective GCS artifact directly

### 11.3 Storage Layout

Recommended GCS layout:

```text
gs://<bucket>/ci-dashboard/jenkins-build-logs/build_id=<build_id>/console_tail.redacted.log.gz
```

Overwrite rule:

- archive rerun overwrites the same object path

## 12. Processing Components

### 12.1 Jenkins Event Worker

New module:

- `src/ci_dashboard/jobs/jenkins_worker.py`

Responsibilities:

1. consume `jenkins-event`
2. parse the CloudEvent
3. extract Jenkins metadata
4. persist raw event audit row
5. create or enrich canonical `ci_l1_builds`

Important non-responsibilities:

- no direct Jenkins console fetch
- no direct GCS upload
- no LLM call

#### 12.1.1 Offset Management

Recommended first-pass strategy:

- manual offset commit after successful DB write
- at-least-once delivery
- dedup through `ci_l1_jenkins_build_events.event_id`
- start with one consumer instance first to avoid early rebalance complexity

### 12.2 Error Log Archive Job

New module:

- `src/ci_dashboard/jobs/archive_error_logs.py`

Responsibilities:

1. query builds where:
- `build_system = 'JENKINS'`
- build result is terminal non-success
- `log_gcs_uri IS NULL`
2. fetch console log from Jenkins
3. keep only the bounded tail window in memory
4. sanitize in memory
5. upload one redacted artifact to GCS
6. update:
- `log_gcs_uri`

Failure behavior:

- on fetch or upload failure:
  - leave `log_gcs_uri` as `NULL`
  - allow safe retry

#### 12.2.1 Concurrency Control

The first slice keeps archive fetch single-threaded on purpose so Jenkins load
is predictable and easy to reason about.

If throughput later becomes a problem, V3 can add a bounded worker pool after
we have real measurements.

#### 12.2.2 Overwrite Rerun

Archive rerun should support explicit overwrite.

Recommended rule:

- scheduled job skips rows with existing `log_gcs_uri`
- manual rerun may force overwrite of the same GCS object path

### 12.3 Error Analysis Job

New module:

- `src/ci_dashboard/jobs/analyze_errors.py`

Responsibilities:

1. query builds where:
- `log_gcs_uri IS NOT NULL`
- `revise_error_l1_category IS NULL`
- `revise_error_l2_subcategory IS NULL`
2. load the redacted artifact
3. run rule engine first
4. call LLM only when rule engine misses
5. update machine classification fields on `ci_l1_builds`
6. never overwrite revise fields

Default query rule:

- `log_gcs_uri IS NOT NULL`
- `revise_error_l1_category IS NULL`
- `revise_error_l2_subcategory IS NULL`

### 12.4 Human Review Path

The first delivery does not require a full review UI.

Minimum viable review path:

- CLI or internal admin endpoint to update human revise fields on
  `ci_l1_builds`

Review behavior in the first slice:

- if a build is not reviewed, human fields remain `NULL`
- if a build is reviewed, human fields overwrite the previous human value
- scheduled AI refresh no longer touches that row by default

## 13. Historical Backfill

### 13.1 CLI Shape

```bash
ci-dashboard backfill-jenkins --start-date 2026-03-24 --end-date 2026-04-24
ci-dashboard backfill-jenkins --last 30
ci-dashboard backfill-jenkins --job "pingcap/tidb/pull_tidb_check" --last 30
```

### 13.2 Backfill Contract

Backfill may use Jenkins REST API to list historical builds and synthesize
event-like payloads, but it must reuse the same canonical merge path as the
realtime worker.

Required properties:

- idempotent re-run
- no dependence on Kafka offsets
- same archive overwrite and analysis rules as realtime ingest

## 14. Deployment

### 14.1 New Kubernetes Resources

1. `KafkaTopic` `jenkins-event`
2. `Deployment` `ci-dashboard-jenkins-worker`
3. `CronJob` `ci-dashboard-archive-error-logs`
4. `CronJob` `ci-dashboard-analyze-errors`
5. `ConfigMap` or `Secret` for:
- Jenkins access settings
- GCS settings and credentials
- LLM provider settings
- allowlist and redaction config

### 14.2 Runtime Configuration

Suggested environment variables:

| Variable | Purpose |
|---|---|
| `CI_DASHBOARD_KAFKA_BOOTSTRAP_SERVERS` | Kafka bootstrap brokers |
| `CI_DASHBOARD_KAFKA_JENKINS_EVENTS_TOPIC` | Jenkins event topic, default `jenkins-event` |
| `CI_DASHBOARD_KAFKA_JENKINS_GROUP_ID` | Jenkins worker consumer group |
| `CI_DASHBOARD_JENKINS_INTERNAL_BASE_URL` | internal Jenkins base URL for server-side log fetch |
| `CI_DASHBOARD_JENKINS_USERNAME` | optional read-only Jenkins user |
| `CI_DASHBOARD_JENKINS_API_TOKEN` | optional read-only Jenkins token |
| `CI_DASHBOARD_JENKINS_PROGRESSIVE_PROBE_START` | initial progressive-text probe offset |
| `CI_DASHBOARD_JENKINS_FINISHED_EVENT_TYPE` | accepted finished-event type |
| `CI_DASHBOARD_GCS_BUCKET` | GCS bucket for redacted log artifacts |
| `CI_DASHBOARD_GCS_PREFIX` | GCS object prefix for archived logs |
| `CI_DASHBOARD_ARCHIVE_LOG_TAIL_BYTES` | tail byte cap |
| `CI_DASHBOARD_LLM_PROVIDER` | provider name for the later analysis slice |
| `CI_DASHBOARD_LLM_MODEL` | concrete model name for the later analysis slice |
| `CI_DASHBOARD_LLM_API_KEY` | provider API key or secret-backed credential |

## 15. File Layout

```text
ci-dashboard/
├── sql/
│   ├── 014_alter_ci_l1_builds_for_v3_jenkins.sql
│   └── 015_create_ci_l1_jenkins_build_events.sql
├── src/ci_dashboard/
│   ├── jobs/
│   │   ├── jenkins_worker.py
│   │   ├── archive_error_logs.py
│   │   ├── analyze_errors.py
│   │   ├── llm_classifier.py
│   │   ├── rule_engine.py
│   │   ├── jenkins_client.py
│   │   ├── gcs_client.py
│   │   └── cli.py
│   └── common/
│       └── config.py
├── error_taxonomy.yaml
└── docs/
    └── ci-dashboard-v3-jenkins-design.md
```

Note:

- `llm_classifier.py` and `rule_engine.py` remain standalone because they define
  clear interfaces and are expected to grow independently
- redaction stays inside `archive_error_logs.py` for the first slice because it
  is still a small helper tied to one caller

## 16. Implementation Phases

### Phase 1: Canonical Build Foundation

1. audit existing `normalized_build_url` data quality using the prerequisite SQL
2. alter `ci_l1_builds` for nullable Prow ids and new V3 fields
3. add Jenkins event audit table
4. update `sync-builds` to merge by `normalized_build_url` first
5. implement Jenkins Event Worker
6. capture at least one real Jenkins finished-event payload and document its
   structure

Exit criteria:

- Jenkins event can create the row first
- later `sync-builds` enriches the same row
- no duplicate canonical rows for sampled builds
- at least one real Jenkins CDEvent captured and payload structure documented

### Phase 2: Secure Log Archival

1. implement Jenkins client
2. implement archive job
3. implement in-memory redaction and one-object GCS upload
4. validate overwrite rerun behavior and retry behavior

Exit criteria:

- sampled error builds archive redacted tails successfully
- overwrite rerun refreshes the effective artifact
- retries are operationally acceptable

### Phase 3: Classification And Review

1. implement taxonomy file and rule engine
2. implement pluggable LLM classifier on sanitized input
3. implement direct AI field update on `ci_l1_builds`
4. implement minimal human-review update path

Exit criteria:

- AI judgment lands on `ci_l1_builds`
- human revise can be written independently
- queries can derive the effective verdict as human-first, AI-second

### Phase 4: Backfill And Tuning

1. implement `backfill-jenkins`
2. backfill a bounded historical window
3. tune tail policy, taxonomy coverage, and redaction quality

## 17. Validation Plan

### 17.1 Merge Correctness

- sample builds where Jenkins event arrived before `sync-builds`
- verify exactly one canonical `ci_l1_builds` row exists per
  `normalized_build_url`
- verify later Prow metadata enriches the same row

### 17.2 Event Audit Completeness

- compare Jenkins completed build count versus `ci_l1_jenkins_build_events`
  within the same time window
- verify duplicate delivery does not create duplicate audit rows

### 17.3 Log Archive Quality

- sample failed builds and confirm:
  - `log_gcs_uri` is written only after sanitization
  - archive rerun overwrites as expected
  - archived tail is useful for diagnosis

### 17.4 Classification Reviewability

- verify AI fields are populated with:
  - category
  - confidence
  - source
  - evidence text
  - provider and model identity when applicable
- verify human revise can be added independently
- verify effective verdict prefers human revise when present
- verify scheduled AI refresh skips rows where revise fields are already present

### 17.5 Security And Access Validation

Before recurring production use, validate:

1. `cloudevents-server` ingress is not unintentionally public
2. Jenkins access path is internal-only
3. GCS IAM restricts artifact access to intended workloads and reviewers
4. LLM input path uses only redacted text and allowlisted metadata

## 18. Rough Cost Estimate

Order-of-magnitude estimate for the first slice:

- GCS storage
  - about `256KB` per archived error build
  - for `100` error builds per day and `90` days retention, roughly `2.3GB`
- LLM calls
  - if `100` error builds per day and `30%` fall back to LLM, roughly `30`
    calls per day
  - at around `2K` tokens per call, cost remains small relative to CI infra
- Kafka cost
  - negligible for this workload size

## 19. Risks And Mitigations

1. Existing `normalized_build_url` quality is insufficient for uniqueness
- mitigation:
  - run duplicate audit before unique index enforcement
  - clean historical collisions before enabling hard uniqueness

2. `sync-builds` remains Prow-id-centric and duplicates Jenkins-first rows
- mitigation:
  - make canonical-key-first merge an explicit implementation gate for Phase 1

3. Tail-only policy misses root-cause evidence for some jobs
- mitigation:
  - keep tail policy configurable
  - allow selective expansion later if needed

4. Redaction quality is too weak for production rollout
- mitigation:
  - block provider calls until redaction policy and provider retention are both
    approved

5. Human revise usage pattern differs from current assumptions
- mitigation:
  - start with one AI slot plus one human slot
  - revisit richer history only after real review behavior appears

## 20. Open Questions

1. Real finished-event payload shape
- which exact fields are reliably present in this Jenkins environment?

2. `normalized_build_url` cleanup scope
- do production rows already have clean one-to-one Jenkins URL normalization, or
  do we need a dedicated cleanup migration first?

3. GCS bucket strategy
- reuse an existing CI bucket or provision a V3-specific bucket?

4. Human review surface
- CLI or admin endpoint only in the first release, or lightweight internal page?

5. Provider retention policy
- what retention and data-handling policy is acceptable across OpenAI, Gemini,
  and Qianwen for sanitized CI evidence?

## 21. Definition Of Done For The Initial V3 Data Foundation

The initial V3 data foundation is done when:

1. Jenkins finished events are durably ingested through the HTTP -> Kafka path.
2. Jenkins finished events can create canonical build rows before
   `sync-builds`.
3. `sync-builds` later enriches the same canonical row rather than creating a
   duplicate.
4. Terminal non-success Jenkins builds archive one usable redacted tail
   artifact.
5. Archive rerun can overwrite the effective artifact safely.
6. LLM input uses only redacted artifact content and allowlisted metadata.
7. AI judgment lands on `ci_l1_builds`.
8. Human revise can be recorded independently on `ci_l1_builds`.
9. Effective verdict is derivable as human-first, AI-second.
10. Later dashboard work can consume the resulting data model without
    redesigning the ingest path.
