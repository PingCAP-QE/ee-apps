# authzd authorization core

This module currently contains the versioned route registry contract and the
initial OpenFGA model/policy examples. It is not yet an Envoy ext_authz service
and is not deployed or connected to production traffic. `observe`, `shadow`,
and `enforce` in the sample registry are declarative values only until the
service implementation defines their response semantics.

## Registry

`policies/registry-v1.json` is ordinary application configuration, not a
Kubernetes CRD. The parser rejects unknown fields, invalid path templates,
ambiguous equal-priority overlaps, malformed objects, and missing checks.
Matching requires host, route identity, exact method, and a normalized path. It
checks external and upstream rewritten paths explicitly, and a miss has no
fallback. A `Store` only publishes fully parsed immutable snapshots.

Static checks use `object`; resource checks use both `object_from` and
`id_from`. A rule may intentionally have one check or several; when a route
requires capability and resource checks, each must be represented as a separate
check. Product decisions such as TiBuild read visibility remain subject to
[ADR 0002](../docs/adr/0002-tibuild-read-visibility.md).

Run the registry suite with:

```sh
go test -race -cover ./...
```

The committed model is validated and exercised by OpenFGA CLI v0.8.2 against
the golden decisions in `tests/authorization.fga.yaml`:

```sh
go install github.com/openfga/cli/cmd/fga@v0.8.2 # requires Go 1.26+
fga model validate --file models/authorization.fga
fga model test --allow-external-files --tests tests/authorization.fga.yaml
```

These tests verify the source model locally; they do not publish it or establish
a production model ID. Do not use this model or sample policy for enforcement
until the model and policy receive the required owner approvals.
