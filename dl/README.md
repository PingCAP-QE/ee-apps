# Download server

It provides download functions for:
- OCI artifact files
- GCS bucket objects

## How to design

Edit [design/design.go](./design/design.go), then regenerate code:

```bash
goa gen github.com/PingCAP-QE/ee-apps/dl/design
goa example github.com/PingCAP-QE/ee-apps/dl/design
```

## How to run locally

```bash
go run ./cmd/server
```

## How to build container image

Build with Skaffold + Ko:

```bash
skaffold build
```

The build config (base image, entrypoint, platforms) is in [skaffold.yaml](./skaffold.yaml).

## Configuration

Three services each use a separate config file:

| Service | Flag | Default | Description |
|---------|------|---------|-------------|
| OCI | `--oci-config` | `oci.yaml` | OCI registry credentials |
| GCS | `--gcs-config` | _(none)_ | Optional, uses ADC by default |

For GCS, the config file is optional. If omitted, the GCS client uses Application Default Credentials (supports GKE Workload Identity, GCE default SA, or `GOOGLE_APPLICATION_CREDENTIALS` env var).

### Target allowlists

The download proxy only talks to explicitly allowlisted targets. An empty
allowlist denies every request.

| Flag | Description |
|------|-------------|
| `--oci-allowed-registries` | Comma-separated OCI registry hosts. IP literals, `localhost`, `*.svc`, `*.local` and cloud metadata hosts are always rejected. |
| `--gcs-allowed-buckets` | Comma-separated GCS bucket names. |
| `--gcs-allowed-prefixes` | Optional comma-separated object key prefixes. When set, keys outside every prefix are rejected. |

Example:

```sh
dl \
  --domain=0.0.0.0:80 \
  --oci-config=/etc/dl/oci.yaml \
  --oci-allowed-registries=us-docker.pkg.dev,hub.pingcap.net,hub-zot.pingcap.net \
  --gcs-allowed-buckets=hotfix-binaries
```

The OCI allowlist is checked before any DNS lookup or registry request. Rejected
targets return a generic 4xx response; upstream host names, TLS errors and
service-account identities are never included in the response body.
