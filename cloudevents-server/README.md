Cloud Events Server
===

## How to run

### With sqlite as backend database:
```bash
go run --tags sqlite3 . -config=configs/example-config-sqlite3.yaml
```

### With MySQL or TiDB as backend database:

```bash
go run . -config=configs/example-config.yaml
```

## Tekton step-log fetching was removed

Tekton failure notifications no longer read pod logs. The fetched target used to
come from the CloudEvent payload (`namespace`, `podName`, `containerName`), and
`POST /events` is unauthenticated, so any producer could turn the notifier into
an arbitrary pod-log reader and have the text delivered to a Lark recipient of
its choosing. The capability was also already dead in production: the Tekton
Dashboard sits behind Envoy Gateway's Google OIDC policy, and the fetch carried
no credentials and refused redirects, so it received a 302 and treated it as an
error.

Cards keep the failed step names and `Terminated.Reason`, the dashboard view
link built from `tekton.dashboard_base_url`, and the rerun command; read the
logs on the Dashboard. The `tekton.failed_step_tail_lines` setting is gone from
the config schema.

## How to release

- manualy build the container images with docker

```bash
skaffold build --default-repo <your image registry>
```

- manualy build the container images with cluster(that has amd64 and arm64 nodes)

```bash
skaffold build --default-repo <your image registry>
```

> more...
