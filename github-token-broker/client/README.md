# github-token-broker client

A small Go client library for the in-cluster GitHub App token broker.

The library only depends on the Go standard library. It does not import
`go-github`, so callers can use any `go-github` version:

```go
import (
    "net/http"

    "github.com/PingCAP-QE/ee-apps/github-token-broker/client/githubauth"
    "github.com/google/go-github/v69/github"
)

transport := githubauth.NewTransport(githubauth.Config{
    BrokerURL:     "http://github-token-broker.github-token-broker.svc",
    TokenFile:     "/var/run/secrets/kubernetes.io/serviceaccount/token",
    FallbackToken: os.Getenv("GITHUB_TOKEN"),
})

client := github.NewClient(&http.Client{Transport: transport})
```

For requests to `api.github.com/repos/{owner}/{repo}/...`, the transport asks
the broker for a repository-scoped installation token:

- `GET`/`HEAD`: `contents: read`, `pull_requests: read`
- other methods: `contents: write`, `pull_requests: write`

Tokens are cached by repository and permission set until five minutes before
expiry. Requests without a repository path, requests to other hosts, and
requests the broker denies use `FallbackToken` when it is configured.

## Configuration

| Field | Description |
| --- | --- |
| `BrokerURL` | Broker base URL; empty disables broker usage. |
| `TokenFile` | Projected ServiceAccount token; defaults to the in-cluster path. |
| `FallbackToken` | Optional static token for non-broker requests. |
| `Base` | Optional base `http.RoundTripper` for GitHub requests. |

## Development

```bash
go test -race ./...
go vet ./...
```
