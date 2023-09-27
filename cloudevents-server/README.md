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
