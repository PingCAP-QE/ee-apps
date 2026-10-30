# Publish-domain legacy contract

Frozen request/response contract for the endpoints moved from `publisher` into
tibuild-v2 (`/tiup/*`, `/tidbcloud/*`). The same file must pass against the old
service and the new one.

Requests are deliberately non-destructive: only payload validation and
lookups of non-existent ids.

```bash
# old service (publisher, ee-ops prod2 route)
hurl --test --variable base_url=https://do2.pingcap.net/publisher tests/hurl/publish-contract/legacy-contract.hurl

# new service (tibuild-v2)
hurl --test --variable base_url=http://localhost:8080 tests/hurl/publish-contract/legacy-contract.hurl
```

Contract rationale, recorded samples and the old request-id → new step-id
mapping live in the Conductor track
`publish_domain_migration_20261029/contract.md`.
