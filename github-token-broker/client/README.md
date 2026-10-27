# github-token-broker client

A small Go client library for the in-cluster GitHub App token broker.

The library only depends on the Go standard library. It does not import
`go-github`, so callers can use any `go-github` version. The example below uses
the latest `go-github/v92` API (`NewClient` returns a client and an error); for
older versions, use that version's `NewClient` signature:

```go
import (
    "log"
    "net/http"
    "os"

    "github.com/PingCAP-QE/ee-apps/github-token-broker/client/githubauth"
    "github.com/google/go-github/v92/github"
)

transport := githubauth.NewTransport(githubauth.Config{
    BrokerURL:     "http://github-token-broker.github-token-broker.svc",
    TokenFile:     "/var/run/secrets/kubernetes.io/serviceaccount/token",
    FallbackToken: os.Getenv("GITHUB_TOKEN"),
})

client, err := github.NewClient(github.WithHTTPClient(&http.Client{Transport: transport}))
if err != nil {
    log.Fatal(err)
}
```

For requests to `api.github.com/repos/{owner}/{repo}/...`, the transport asks
the broker for a repository-scoped installation token. The requested permission
is derived from the API resource and HTTP method:

- `/git/*`, `/branches`, `/commits`, `/compare`, `/contents`, `/tags` → `contents`
- `/pulls` → `pull_requests`
- `/issues` → `issues`
- `/actions` → `actions`
- unknown repository endpoints → `contents`
- `GET`/`HEAD` request `read`; other methods request `write`

For example, creating a git tag (`POST /git/tags`) requests only
`contents: write`, while creating a pull request requests
`pull_requests: write`.

Tokens are cached by repository and permission set until five minutes before
expiry. Requests without a repository path, requests to other hosts, and
requests the broker denies use `FallbackToken` when it is configured.

## Pinned (command-scoped) mode

Some callers need endpoints that are not repository-scoped
(`/orgs/{org}/members`, `/users/{login}`) or endpoints whose permission cannot
be derived from the path (`/repos/{owner}/{repo}/collaborators` needs
`administration`). Set `Repo` and `Permissions` to mint one token for an
explicit scope and use it for every GitHub API request:

```go
transport := githubauth.NewTransport(githubauth.Config{
    BrokerURL:   "http://github-token-broker.github-token-broker.svc",
    Repo:        "example-org/example-repo",
    Permissions: map[string]string{"members": "read", "administration": "read"},
    OnFallback: func(repo string, err error) {
        // Report silent degradation to a static token.
    },
})
```

In pinned mode the token is only attached to `api.github.com` requests, so
signed download URLs stay unauthenticated. `OnFallback` is called whenever the
broker request fails and `FallbackToken` takes over.

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
