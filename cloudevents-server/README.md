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

## How to release

- manualy build the container images with docker

```bash
skaffold build --profile local-docker --default-repo <your image registry>
```

- manualy build the container images with cluster(that has amd64 and arm64 nodes)

```bash
skaffold build --default-repo <your image registry>
```

