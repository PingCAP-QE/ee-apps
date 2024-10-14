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
rm -rf gen cmd tiup.go
goa gen github.com/PingCAP-QE/ee-apps/publisher/design
goa example github.com/PingCAP-QE/ee-apps/publisher/design
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
