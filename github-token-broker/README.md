# github-token-broker

In-cluster service that mints least-privilege GitHub App installation tokens
for CI workloads. It replaces long-lived Personal Access Tokens and user SSH
keys for automation, including repositories in SAML SSO enforced organizations,
where user credentials are blocked but GitHub App installation tokens are not.

## How it works

1. A workload (CI step, service, or agent) calls `POST /v1/token` with its
   Kubernetes ServiceAccount token.
2. The broker authenticates the caller with the Kubernetes `TokenReview` API.
3. The caller is authorized against the configured policy (default deny).
4. The broker resolves the GitHub App installation for the target repository
   and mints an installation token. Tokens can be narrowed to specific
   repositories and permissions.
5. If the App is not installed for an owner, the broker can fall back to a
   configured PAT (`source: "pat"` in the response).

The GitHub App private key only ever lives in the broker pod (mounted from a
Secret). Callers receive short-lived installation tokens (about one hour).

The implementation uses
[`google/go-github`](https://github.com/google/go-github) and
[`bradleyfalzon/ghinstallation`](https://github.com/bradleyfalzon/ghinstallation)
for App authentication and API calls. The HTTP API is designed with
[Goa](https://goa.design) (`internal/service/design/design.go`) and the
transport/server skeleton is generated (`go generate ./internal`); business
logic lives in `internal/service/impl`. Images are built with
[ko](https://ko.build) (no Dockerfile).

Metrics use the OpenTelemetry API with a Prometheus exporter (`/metrics`), so
switching to an OTLP exporter is a local change.

## API

```http
POST /v1/token
Authorization: Bearer <projected ServiceAccount token>
Content-Type: application/json

{
  "repo": "example-org/example-repo",
  "repositories": ["example-repo"],
  "permissions": {"contents": "read"}
}
```

`repositories` and `permissions` are optional:

- omitted `permissions` defaults to `{"contents": "read"}`;
- `repositories` omitted means the token covers the whole installation (useful
  for submodules). For requests that include any `write` permission the broker
  narrows the token to the target repository unless `repositories` is given.

Response:

```json
{
  "token": "ghs_...",
  "expires_at": "2026-10-20T08:00:00Z",
  "installation_id": 65036159,
  "source": "app"
}
```

## Policy

Policies are denied by default. Rules can be defined inline in the main
configuration or loaded from a separate file:

```yaml
policy:
  # A mounted Secret/ConfigMap path; takes precedence over inline rules.
  file: /etc/github-token-broker/policy/policy.yaml
```

The policy file contains a top-level `rules:` list:

```yaml
rules:
  - caller:
      namespace: ci
      service_account: checkout
    allow:
      - owner: example-org
        repositories: ["*"]
        max_permissions:
          contents: read
```

Mount the policy from a Secret or ConfigMap when it must not live in the
GitOps repository. Inline rules look like this:

```yaml
policy:
  rules:
    - caller:
        namespace: ci
        service_account: checkout
      allow:
        - owner: example-org          # or "*" for any owner
          repositories: ["*"]       # repository names, ["*"], or [] for all
          max_permissions:
            contents: read
```

Requested permissions must be a subset of `max_permissions`; write access is
only granted when the policy allows it.

### Evaluation semantics

For a caller, the broker evaluates all matching `allow` entries. An entry
matches when:

- the owner matches (`*` matches any owner);
- the target repository and any `repositories` narrowing are covered by the
  entry's `repositories` list (`["*"]` or an empty list covers all repositories
  of the owner);
- `max_permissions` covers the requested permissions (`write` implies `read`).

The request is allowed when **any** matching entry grants it. Entries for the
same owner are additive, so a policy can grant `contents: write` to one
repository and `contents: read` to another under the same owner.

When no entry grants the request, the broker returns HTTP 403 with one of:

- `caller is not allowed`: the caller has no rule, or no rule matches the owner;
- `repository is not allowed: <repo>`: the caller has rules, but no entry covers
  the target repository or the requested `repositories` narrowing;
- `permission is not allowed: <permission>=<level> (max <level>)`: the
  repository is covered, but the requested permission exceeds the grant.

Error responses never include token material.

## Configuration

See [config.example.yaml](./config.example.yaml). Notable settings:

- `github.app_id`, `github.private_key_path`: GitHub App credentials.
- `github.fallback_pat_file`: optional PAT for owners where the App is not
  installed (prefer a mounted Secret over inline values).
- `cache.ttl_margin`: stop using a cached token this long before expiry.
- `rate_limit.per_minute`: per-caller fixed-window limit (0 disables it).
- `reload.interval`: how often the configuration and policy files are polled
  for changes (default `5s`; set `reload.disabled: true` to turn it off).

## Configuration reload

The broker polls the configuration file (and `policy.file`, when configured)
every `reload.interval` and swaps valid configurations atomically. Invalid
files are rejected and the previous configuration is kept.

Hot-reloadable settings: policy rules, fallback PAT, rate limit, cache TTL
margin, and the policy file path. Server listener settings and GitHub App
credentials/Auth settings require a restart (a warning is logged when they
change).

Kubernetes ConfigMap/Secret volumes are mounted as directories (not
`subPath`), so projected updates are visible to the poller.

## Health checks

- `GET /healthz` (liveness): the process is running.
- `GET /readyz` (readiness): the configuration is loaded **and** the Kubernetes
  API is reachable via the broker's ServiceAccount (`GET /version`). Failures
  return `503` and a `not_ready` error.

Both endpoints are part of the Goa design and generated transport.

## Metrics and rate limiting

`GET /metrics` exposes Prometheus metrics:

- `github_token_broker_requests_total{source,result}`
- `github_token_broker_errors_total{reason}`
- `github_token_broker_rate_limited_total`
- `github_token_broker_mint_seconds` (histogram)

Requests are limited per calling ServiceAccount (`namespace/serviceaccount`) in
a fixed one-minute window. Rejected requests get HTTP 429 with `Retry-After`.

## Helm chart

The chart lives in [`../charts/github-token-broker`](../charts/github-token-broker):

```bash
helm template github-token-broker ../charts/github-token-broker --namespace ci
```

## Local development

Requires Go 1.27+.

```bash
go generate ./internal   # regenerate the Goa transport after changing the design
go test ./...
go run ./cmd/broker -config config.example.yaml
```

The broker needs a reachable Kubernetes API for `TokenReview`; for local tests
set `auth.kubernetes_api_url` and `auth.token_file` to a kubeconfig-backed API
server token, or exercise the packages with `go test`.

## Container build

Images are built with [ko](https://ko.build) through Skaffold; there is no
Dockerfile to maintain:

```bash
skaffold build --push=false -f skaffold.yaml
```

The same Skaffold config publishes two images:

| Image | Purpose |
|---|---|
| `github-token-broker` | the broker server (`distroless/static`) |
| `github-token-broker-git-credential` | git credential helper used by CI steps; Alpine base so a Tekton step can install the statically linked helper into a build pod's shared `$HOME` |

## Git credential helper

`cmd/git-credential` is a
[git credential helper](https://git-scm.com/docs/gitcredentials) for workloads
that need GitHub credentials without a PAT — Tekton checkout steps and build
steps that fetch private Go, Cargo or npm dependencies.

```bash
# invoked by git as: credential.helper = /path/to/git-credential
git-credential get      # reads the request from stdin, prints username/password
git-credential erase    # drops the cached token after a rejected credential
```

It authenticates to the broker with the pod ServiceAccount token, requests
`contents: read` (configurable) for the repository in the git request and caches
each token on disk until 5 minutes before expiry, so a build longer than one
hour keeps working without minting on every git operation.

| Variable | Default | Purpose |
|---|---|---|
| `GIT_AUTH_BROKER_URL` | `http://github-token-broker.github-token-broker.svc` | broker base URL |
| `GIT_AUTH_BROKER_TOKEN_FILE` | `/var/run/secrets/kubernetes.io/serviceaccount/token` | caller ServiceAccount token |
| `GIT_AUTH_PERMISSIONS` | `contents=read` | `name=level` pairs separated by `;` |
| `GIT_AUTH_HOSTS` | `github.com` | comma-separated hosts allowed to receive a token |
| `GIT_AUTH_CACHE_DIR` | user cache dir | minted token cache |
| `GIT_AUTH_FALLBACK_CREDENTIALS` | unset | optional `.git-credentials` file used when the broker denies the request |
| `GIT_AUTH_STATE_FILE` | unset | records the credential source (`app` or `pat`) for auditing |
| `GIT_AUTH_TIMEOUT` | `10s` | per-request timeout |

The helper never receives the App private key and never logs token material.

## Security notes

- Tokens are never logged; only the caller, repository, permissions and source
  are recorded.
- The App private key must only be mounted into this service.
- Always keep `max_permissions` minimal; the GitHub App itself has broad
  permissions, so the broker narrows each token.
- The broker authenticates callers by ServiceAccount; deploy it in a namespace
  whose ServiceAccounts are trusted, and restrict network access with a
  NetworkPolicy.
