# Roster

Roster syncs company employee and group data into the database for cost attribution,
CI ownership lookup, and issue routing.

The initial design is documented in [docs/schema-design.md](docs/schema-design.md).

## Local Job Entry

The sync command is:

```bash
python -m roster.jobs.cli sync-roster
```

Validate Lark data without writing DB:

```bash
python -m roster.jobs.cli validate-lark
```

Compare synced roster rows with historical employee identity tables:

```bash
python -m roster.jobs.cli validate-history --details-limit 20
```

Database configuration accepts either a SQLAlchemy URL:

```bash
ROSTER_DB_URL='mysql+pymysql://user:pass@host:4000/db?charset=utf8mb4'
```

or TiDB fields. For compatibility with the existing CI Dashboard DB secret,
both `ROSTER_TIDB_*` and plain `TIDB_*` names are accepted:

```bash
ROSTER_TIDB_HOST=...
ROSTER_TIDB_PORT=4000
ROSTER_TIDB_USER=...
ROSTER_TIDB_PASSWORD=...
ROSTER_TIDB_DB=...
```

Lark sync is enabled when both of these are present:

```bash
ROSTER_LARK_APP_ID=...
ROSTER_LARK_APP_SECRET=...
```

Optional Lark settings:

```bash
ROSTER_LARK_GITHUB_CUSTOM_ATTR_ID=...
ROSTER_LARK_ROOT_DEPARTMENT_ID=0
```

## Kubernetes CronJob

Build the jobs image from this directory:

```bash
docker build -f Dockerfile.jobs -t ghcr.io/pingcap-qe/ee-apps/roster-jobs:<tag> .
```

Render a CronJob manifest:

```bash
./scripts/render_roster_sync_cronjob.sh \
  --image ghcr.io/pingcap-qe/ee-apps/roster-jobs:<tag> \
  --db-secret ci-dashboard-eq-prd-insight-db \
  --lark-secret roster-lark \
  --suspend true \
  > /tmp/roster-sync.yaml
```

The DB secret should contain `ROSTER_DB_URL`, `CI_DASHBOARD_DB_URL`,
`ROSTER_TIDB_*`, or `TIDB_*` fields. The Lark secret should contain `ROSTER_LARK_APP_ID` and
`ROSTER_LARK_APP_SECRET`, plus optional `ROSTER_LARK_GITHUB_CUSTOM_ATTR_ID`.
