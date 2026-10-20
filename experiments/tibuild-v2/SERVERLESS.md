# Serverless devbuild

Products `tidb-cse`, `pd-cse`, `tidb-operator-cse`, `cassandra-on-mysql` and
`qdrant-on-tidb` use Tekton with the `release` artifacts profile. The live
`product_repo_map` must contain each `tidbcloud/<product>` mapping: update the
ExternalSecret configuration in `PingCAP-QE/ee-ops` together with this release.
No database fields or migrations are required.

Use the existing `/devbuild trigger` command and build ID queries:

```text
/devbuild trigger --product tidb-cse --version v8.5.3 --gitRef branch/main-8.5-keyspace --platform linux
```

The compatibility `version` value is received, saved, passed to CloudEvents and
shown as request metadata. It never overrides Git version inference or image
tags. Actual version, fixed commit SHA, image tags, platform artifacts and
checksum file addresses reuse existing PipelineRun parameters and artifact results.
The actual version is read from the existing `get-release-ver` TaskRun `version`
result; the Pipeline results contract is unchanged.

Supported platforms: `linux` (two events: amd64 and arm64), `linux/amd64`,
`linux/arm64`. Empty platform defaults to Linux on both architectures. Unsupported
engines, editions, hotfix requests and enterprise plugins fail validation before
an event is submitted. Existing products retain their platform and command rules.

`branch/name`, `tag/name`, `pull/N`, `commit/<40-character SHA>` and a raw SHA are
accepted. Creation resolves and stores the actual commit; annotated tags are
peeled to the commit. Resolution/access errors return HTTP 400. The same stored
SHA is sent for every platform and reused on reruns. `githubRepo` can select a
fork; `paramProduct` keeps component and artifact paths independent of its name.

Events add `paramProduct`, `paramGitRevision` and compatibility `paramVersion`
without removing existing extensions or GitHub-shaped payloads. CI uses a
single-platform template per event, with the existing CloudEvent context retained
for result collection. Legacy canonical events remain routable. Devbuild writes
only `us-docker.pkg.dev/pingcap-testing-account/dev/tidbcloud/<product>/...` and
never invokes TiUP, image delivery or environment deployment.

Reconciliation refreshes pipelines and artifact reports even while the overall
status stays `PROCESSING`. A request succeeds only after all requested platforms
succeed with non-empty image and package/checksum results. Missing platforms keep
it incomplete; an empty result cannot turn a successful Tekton run into build
success. Request `version` remains distinct from the Git-derived build version.

Validation: regenerate `go generate ./internal/`; run `go test ./...` and build
both commands in this module, then independently test/build `chatops-lark`.
`go test -short ./...` skips container-dependent integration tests when a local
Docker daemon is unavailable. Before rollout, verify branch/tag/PR/SHA requests,
single and dual-platform completion, artifact download/checksums and final Lark
cards against the staged service and Tekton route revisions. Do not enable source
publisher cutovers or formal distribution on the basis of unit tests alone.

## Isolated native integration test

`TestServerlessStagedDevbuild` is opt-in and uses the generated Goa HTTP client
and server, an in-memory SQLite database, real GitHub ref resolution, an isolated
Tekton EventListener, the Dashboard API reconciler and rendered Lark card JSON.
It creates actual dev artifacts. Configure the listener with copies of the
Serverless triggers carrying **different `type` labels** from the live listeners,
a pinned artifacts revision, a validation-only pipeline, local checkout baseline
tags and delivery disabled. Do not point this test at the production listener.
No production database or Lark messages are used.

Supply `GH_TOKEN` at runtime and set:

```sh
export SERVERLESS_STAGE_EVENTLISTENER=http://127.0.0.1:18080
export SERVERLESS_STAGE_DASHBOARD=http://127.0.0.1:19097
export SERVERLESS_STAGE_CONFIRM=isolated-dev-only
export SERVERLESS_STAGE_PRODUCT=cassandra-on-mysql
export SERVERLESS_STAGE_GIT_REF=branch/master
export SERVERLESS_STAGE_PLATFORM=linux
# Optional existing download endpoint and evidence output directory:
export SERVERLESS_STAGE_DOWNLOAD_URL=https://prow.tidb.net/dl/oci-file
export SERVERLESS_STAGE_OUTPUT=/tmp/serverless-stage-evidence
go test ./internal/service/impl -run '^TestServerlessStagedDevbuild$' -count=1 -v -timeout 50m
```

Repeat with PR, tag and commit inputs after their Git refs exist. The test checks
fixed SHA, one event/run per requested platform, complete result collection,
Git-derived version independent of `v99.0.0` request metadata, API artifact URLs
and rendered card content. Save the evidence before the cluster pruner removes
runs. Tag creation on merged cutover commits, deployed service/chatops images,
actual Lark delivery and formal controller distribution remain rollout checks.
