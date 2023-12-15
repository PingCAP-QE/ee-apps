# Download server

It provide the download functions:
- List or download Object data from KS3 bucket.
- List or download File from OCI artifact.

## How to design

go to [design/design.go](./design/design.go)

## How to generate code from design

> we need the goa tool

run:
```bash
rm -rf gen cmd oci.go
goa gen github.com/PingCAP-QE/ee-apps/dl/design
goa example github.com/PingCAP-QE/ee-apps/dl/design
```

## How to run

```bash
go run ./cmd/server
```
