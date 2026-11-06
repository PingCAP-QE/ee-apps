# Publish-domain legacy contract

Frozen request/response contract for the endpoints moved from `publisher` into
tibuild-v2. Two files share the assertions but differ in addressing:

- `legacy-contract.hurl` — the pre-migration paths (`/tiup/*`,
  `/tidbcloud/*`). Run it against `publisher`, or against a gateway that
  rewrites those prefixes onto tibuild-v2.
- `tibuild-v2-contract.hurl` — the paths tibuild-v2 serves directly
  (`/api/v2/publish/tiup/*`, `/api/v2/tidbcloud/*`). Run it against a local or
  port-forwarded tibuild-v2.

Requests are deliberately non-destructive: only payload validation and
lookups of non-existent ids.

```bash
# old service (publisher, ee-ops prod2 route)
hurl --test --variable base_url=https://do2.pingcap.net/publisher tests/hurl/publish-contract/legacy-contract.hurl

# new service (tibuild-v2)
hurl --test --variable base_url=http://localhost:8080 tests/hurl/publish-contract/tibuild-v2-contract.hurl
```

Contract rationale, recorded samples and the old request-id → new step-id
mapping live in the Conductor track
`publish_domain_migration_20261029/contract.md`.
