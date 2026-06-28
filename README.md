# EE Applications

Internal tooling and services for PingCAP Engineering Effectiveness.

## Projects

| Directory | Description | Language | Docs |
|-----------|-------------|----------|------|
| [`tibuild/`](tibuild/) | Build orchestration platform (DevBuild, Hotfix, Tekton/Jenkins triggers) | Go | [README](tibuild/README.md) |
| [`experiments/tibuild-v2/`](experiments/tibuild-v2/) | Next-gen tibuild with Goa + Ent ORM | Go | [README](experiments/tibuild-v2/README.md) |
| [`cloudevents-server/`](cloudevents-server/) | CloudEvents router — receives Tekton events, dispatches via Kafka | Go | [README](cloudevents-server/README.md) |
| [`publisher/`](publisher/) | Artifact publisher — TiUP packages, tarballs, container images | Go | [README](publisher/README.md) |
| [`dl/`](dl/) | Download server for OCI artifacts and KS3 objects | Go | [README](dl/README.md) |
| [`chatops-lark/`](chatops-lark/) | Lark (Feishu) ChatOps bot — devbuild, hotfix, cherry-pick commands | Go | [README](chatops-lark/README.md) |
| [`change-insight/`](change-insight/) | Configuration change analysis platform | Go | [README](change-insight/README.md) |
| [`ci-dashboard/`](ci-dashboard/) | CI metrics dashboard — FastAPI backend, React frontend | Python | [README](ci-dashboard/README.md) |
| [`cost-insight/`](cost-insight/) | Cloud cost collection and attribution (GCP BigQuery, AWS) | Python | [README](cost-insight/README.md) |
| [`roster/`](roster/) | Team roster sync from Lark for cost attribution | Python | [README](roster/README.md) |
| [`mcp-servers/`](mcp-servers/) | MCP servers — knowledge base search and PR analysis | Python | — |
| [`charts/`](charts/) | Helm charts for all deployable services | Helm | — |

## Repository Structure

Each subproject is an independent module with its own build system. There is no root-level `Makefile` or `go.work` — always `cd` into the subproject first.

## Quick Start

```bash
# Go services
cd tibuild && make build

# Go services (no Makefile)
cd experiments/tibuild-v2 && go test ./... && go build ./cmd/tibuild

# Python services
cd ci-dashboard && make test && make lint

# Container build (any service with skaffold.yaml)
skaffold build --push=false -f tibuild/skaffold.yaml
```

## CI

Pull requests trigger path-filtered builds — only changed subprojects are tested. See [`.github/workflows/`](.github/workflows/) for details.

## License

[MIT](LICENSE)
