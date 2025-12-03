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

## Health Check Endpoints

The service provides health check endpoints for monitoring and load balancers:

- `GET /api/v2/healthz` - Health check endpoint
- `GET /api/v2/livez` - Liveness check endpoint

Both endpoints return a boolean `true` value with HTTP 200 status when the service is healthy.

## File Structure

> WIP
