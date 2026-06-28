# AGENTS.md

## Repository Overview

Monorepo for PingCAP Engineering Effectiveness (EE) tools. Each subdirectory is an **independent module** — there is no `go.work` file. Go modules and Python projects coexist. CI is path-filtered: only changed subprojects are tested.

## Project Map

| Directory | Language | Framework | Binary Entrypoints |
|-----------|----------|-----------|-------------------|
| `tibuild/` | Go | Gin + GORM + Swagger | `cmd/tibuild/` |
| `experiments/tibuild-v2/` | Go | Goa v2 + Ent ORM | `cmd/tibuild/`, `cmd/tibuild-cli/` |
| `cloudevents-server/` | Go | Gin + Kafka + Ent | `cmd/server/` |
| `publisher/` | Go | Goa + Kafka + Redis | `cmd/publisher/`, `cmd/worker/`, `cmd/publisher-cli/` |
| `dl/` | Go | Goa | `cmd/server/`, `cmd/server-cli/` |
| `chatops-lark/` | Go | Lark WebSocket | `cmd/server/` |
| `change-insight/` | Go | Gin + Viper | `main.go` (root) |
| `ci-dashboard/` | Python | FastAPI + React | uvicorn + npm |
| `cost-insight/` | Python | BigQuery/GCS | jobs only |
| `roster/` | Python | Lark API sync | jobs only |
| `mcp-servers/*/` | Python | MCP protocol | Flask/Next.js |

## Build & Test Commands

**Always `cd` into the subproject directory first.** There are no root-level build commands.

### Go projects (tibuild, cloudevents-server, publisher, dl, chatops-lark, change-insight)
```bash
go test ./...
go build ./...
```

### tibuild
```bash
cd tibuild
make build    # swagger + test + go build
make test     # go test ./...
make swagger  # regenerates Swagger docs
```

### experiments/tibuild-v2
```bash
cd experiments/tibuild-v2
go test -v ./...
go build ./cmd/tibuild && go build ./cmd/tibuild-cli
```
CI workflow (`.github/workflows/ci-tibuild-v2.yaml`) runs exactly these commands.

### Python projects (ci-dashboard, cost-insight, roster)
```bash
cd <project>
make test     # pytest
make lint     # ruff check
```
ci-dashboard also: `make test-cov` (90% coverage threshold), `make api` (dev server).

## Code Generation

**Run `go generate` after modifying these source files:**

| Project | Trigger file | Command | What it generates |
|---------|-------------|---------|-------------------|
| `publisher/` | `internal/service/design/design.go` | `go generate ./internal/` | Goa service interfaces + HTTP transport |
| `dl/` | `design/design.go` | `go generate ./...` | Goa service interfaces |
| `experiments/tibuild-v2/` | `internal/service/design/design.go` + `internal/database/schema/*.go` | `go generate ./internal/` | Goa + Ent ORM code |
| `cloudevents-server/` | `ent/schema/*.go` | `go generate ./ent/` | Ent ORM code |

## Container Builds

All services use **Skaffold v4beta6** with multi-arch (`linux/amd64,linux/arm64`).

```bash
# Build without push (CI)
skaffold build --push=false -f <subproject>/skaffold.yaml

# Build with push (release)
skaffold build -f <subproject>/skaffold.yaml
```

Build methods: Dockerfile (tibuild, dl, cloudevents-server, ci-dashboard, cost-insight, roster) or Ko (chatops-lark, publisher, tibuild-v2).

## Helm Charts

Located in `charts/`. Published to `oci://ghcr.io/pingcap-qe/ee-apps/charts`.
Charts: `chatops-lark`, `ci-dashboard`, `cloudevents-server`, `dl`, `publisher`, `publisher-worker`, `tibuild-v2`.

## CI Workflows (`.github/workflows/`)

| File | Trigger | Scope |
|------|---------|-------|
| `ci.yaml` | PR to main | Skaffold build (no push) for changed services |
| `ci-tibuild-v2.yaml` | PR to main (tibuild-v2 changes) | `go test` + `go build` |
| `release.yaml` | Push to main/tags | Skaffold build + push |
| `charts-release.yaml` | Push to main (chart changes) | Helm package + push to OCI |
| `weekly-release.yaml` | Weekly cron (Sunday UTC) | Creates `vYYYY.M.D` tag + release |

## Git Commit Convention

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description
```

- **Types**: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`
- **Scope**: subproject name — `tibuild`, `tibuild-v2`, `ci-dashboard`, `cost-insight`, `publisher`, `dl`, `chatops-lark`, `cloudevents-server`, `change-insight`, `roster`, `charts`
- **No scope** for cross-cutting changes (e.g. `fix: auto fixes from pre-commit.com hooks`)
- **Lowercase** description, no trailing period
- English preferred; Chinese acceptable for clarity

Examples:
```
feat(notifier): show all PipelineRuns instead of only the first one
fix(ci-dashboard): keep sticky flaky table cells opaque
docs: add AGENTS.md and update README with project overview
```

## Pre-commit Hooks

```yaml
- end-of-file-fixer
- trailing-whitespace
- gitleaks  # secret scanning — blocks commits with leaked secrets
```

## Key Patterns

- **Goa framework** (publisher, dl, tibuild-v2): Design-first API generation. Edit `design/*.go`, run `go generate`, generated code lands in `gen/` or `service/gen/`.
- **Ent ORM** (cloudevents-server, tibuild-v2): Schema files in `ent/schema/` or `database/schema/`. Run `go generate` after schema changes.
- **CloudEvents**: tibuild sends Tekton events → cloudevents-server routes via Kafka → publisher-worker consumes.
- **tibuild-v2 notification**: Ent hooks on DevBuild updates trigger Lark webhook notifications when status reaches terminal state (success/failure/error/aborted). `NotificationInfo.PipelineRuns` carries all pipeline run data.
- **TektonStatus.Pipelines** (in schema `types.go`): Despite the name `Pipelines`, each entry represents a **PipelineRun**, not a pipeline definition.
