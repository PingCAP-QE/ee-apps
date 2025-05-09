# Publisher server

It provide the publisher functions:
- Publish TiUP pacakge from OCI artifact or http file url.
- Publish tarball to object storage from OCI artifact.

## How to design

go to [design/design.go](./design/design.go)

## How to generate code from design

> we need the goa tool

run:
```bash
go generate ./...
```

## How to run


### Start the API server

```bash
go run ./cmd/publisher -config=config-publisher.yaml --debug --domain 0.0.0.0:8080
```

### Start the worker instance.

```bash
go run ./cmd/worker --config=config-worker.yaml --debug
```

### Test with the client CLI

```bash
go run ./cmd/publisher-cli -url=http://localhost:8080 tiup request-to-publish -body '{ "artifact_url": "hub.pingcap.net/pingcap/tidb/package:master_linux_amd64" }'
go run ./cmd/publisher-cli -url=http://localhost:8080 fileserver request-to-publish -body '{ "artifact_url": "hub.pingcap.net/pingcap/tidb/package:master_linux_amd64" }'
```
