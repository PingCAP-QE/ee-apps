# TiBuild

It provide the CD functions for PingCAP, Welcome bros!
- trigger Dev-build runs for custom requirement.
- publish/deliver artifacts.


## Technologies
+ Backend:
  - Golang & GOA(for design)
  - Database: ENT with TiDB/MySQL.
+ Frontend:
  - [Create-React-App for framework](https://github.com/facebook/create-react-app)
  - [Material-UI/MUI for components](https://github.com/mui-org/material-ui)
  - [Axios for remote procedure call](https://github.com/axios/axios)
+ Deployments: Docker & Kubernetes
+ Some core dependencies:
  - [Github](https://github.com/google/go-github)
  - [Config](https://github.com/jinzhu/configor)

## Quick Start

### Design the backend server

go to [internal/service/design/design.go](./internal/service/design/design.go) and update it.

### Design the database

go to [internal/database/schema/](./internal/database/schema/) and update them.

### Generate code from design

> we need the goa tool

run:
```bash
go generate -x ./...
```

### Run it

> Please prepare a `config.yaml` file before start or you
> can passing the `--config` option when run the server.

```bash
go run ./cmd/server
```

After waiting a few seconds, application is available and can be visited in the browser:[localhost:8080](http://localhost:8080/)

## Testing

```bash
go test ./...
```

Integration tests (e.g. `internal/service/tests/artifact_test.go`) spin up a
real registry via [testcontainers](https://golang.testcontainers.org/), so they
require a running Docker daemon and are skipped with `go test -short ./...`.

When using [colima](https://github.com/abiosoft/colima) as the Docker backend
on macOS, set the following env vars so testcontainers can reach the daemon and
mount its socket into containers:

```bash
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock \
TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock \
go test ./...
```

## File Structure

> WIP

## Lark build notifications

Notifications use Card JSON 2.0 (Lark client 7.20 or newer). Image results are
grouped by repository, then platform, then tag. Multi-architecture tags appear
first within each repository. Each tag links to the original complete Docker
reference, without adding a protocol prefix. Binary results are grouped by
platform and filename; available SHA256 files are attached to
their corresponding binary.

Card text uses native `i18n_content` fields (`en_us` and `zh_cn`) in
`internal/service/impl/lark_templates/devbuild-notify.yaml.tmpl`. Lark selects
text according to each viewer's client language; English is the fallback.
There is no server-side language setting or Go translation table. Add or edit
translations in the template. Artifact identifiers and pipeline status values
remain unchanged.

Binary links use `tekton.oci_file_download_url`, with the same repository, tag
and file query parameters as the DevBuild API. Without a download service URL,
notifications display filenames without a download hyperlink.

Users can right-click an image tag link to copy its full `repository:tag`
reference, or a binary filename / SHA256 hyperlink to copy its download URL.
Bare Docker reference links were verified in the receiving Lark client; they
are intended for copying and do not represent HTTP download endpoints.
No buttons, callbacks, or extra HTTP routes are needed.
