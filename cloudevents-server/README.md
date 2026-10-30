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

## Tekton failed-step log fetching

The notifier treats CloudEvent namespace/pod/container fields as untrusted input,
not as Kubernetes-admission-validated objects. Before requesting logs it validates
namespace/container DNS labels and the pod DNS subdomain, then constructs only:

```text
<dashboard_base_url>/api/v1/namespaces/<namespace>/pods/<pod>/log
    ?container=<encoded-container>&tailLines=<configured-integer>
```

The configured dashboard path prefix is preserved. The base URL must be an
absolute HTTP(S) URL without userinfo, query or fragment. Invalid metadata causes
no HTTP request. Failed log retrieval remains best-effort: the failure notification
still contains its existing status information, but no fetched log text.

HTTPS uses Go's normal CA and hostname verification; there is no insecure TLS
fallback. Install any required private CA in the container's trusted certificate
bundle (or configure Go's `SSL_CERT_FILE`/`SSL_CERT_DIR`) before rollout. Plain HTTP
remains supported for existing internal dashboard configurations; it does **not**
provide TLS authentication or encryption. This transport change is specific to
step-log fetching, not other outbound clients.

Log requests do not follow redirects, have a 10-second total client timeout, and
read at most 1 MiB plus one lookahead byte. Non-2xx, oversized or failed responses
are errors, not content for notification cards; response bodies are always closed.
Ordinary names, dashboard prefixes, tail-line settings, and #781's notification
receiver rules are unchanged. No new configuration field or dependency is needed.

This is path/query/TLS hardening, **not event authentication or authorization**.
A syntactically valid namespace/pod/container is not proof of a trusted producer
or ownership of that pod. The configured dashboard's read permissions, `/events`
producer authentication and notification-recipient authorization need separate
controls; do not claim that this change prevents every forged-event log read.
Deployment and security re-verification are still required after code merge.

Hermetic regression tests use local HTTP/TLS servers and existing PipelineRun /
standalone TaskRun fixtures, without fetching real logs or sending Lark messages:

```bash
cd cloudevents-server
go test ./...
go test -race ./pkg/events/tekton
go build ./...
go vet ./...
```

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
