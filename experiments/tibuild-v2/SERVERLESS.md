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
checksum file addresses are reported from pipeline results.

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
