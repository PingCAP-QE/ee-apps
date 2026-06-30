# Download server

It provides download functions for:
- OCI artifact files
- KS3 bucket objects
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
| KS3 | `--ks3-config` | `ks3.yaml` | KS3 endpoint and credentials |
| GCS | `--gcs-config` | _(none)_ | Optional, uses ADC by default |

For GCS, the config file is optional. If omitted, the GCS client uses Application Default Credentials (supports GKE Workload Identity, GCE default SA, or `GOOGLE_APPLICATION_CREDENTIALS` env var).
