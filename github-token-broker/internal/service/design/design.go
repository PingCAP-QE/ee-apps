package design

import (
	//nolint:staticcheck // Goa designs use a dot import by convention.
	. "goa.design/goa/v3/dsl"
)

var _ = API("github-token-broker", func() {
	Title("GitHub Token Broker")
	Description("Mints least-privilege GitHub App installation tokens for CI workloads.")
	Version("0.1.0")

	Server("github-token-broker", func() {
		Host("development", func() {
			URI("http://localhost:8080")
		})
	})
})

// TokenPayload is the token request body.
var TokenPayload = Type("TokenPayload", func() {
	Description("Token request.")

	Attribute("repo", String, "Target repository in owner/name form.", func() {
		Pattern(`^[^/]+/[^/]+$`)
		Example("example-org/example-repo")
	})
	Attribute("repositories", ArrayOf(String), "Optional repository names to narrow the token to.")
	Attribute("permissions", MapOf(String, String), "Optional GitHub permissions subset.")
	Required("repo")
})

// TokenResult is the token response body.
var TokenResult = Type("TokenResult", func() {
	Attribute("token", String, "Installation access token.")
	Attribute("expires_at", String, "Token expiry (RFC3339).", func() {
		Format(FormatDateTime)
	})
	Attribute("installation_id", Int64, "GitHub App installation ID.")
	Attribute("source", String, "Token source.", func() {
		Enum("app", "pat")
	})
	Required("token", "source")
})

// Token service issues installation tokens.
var _ = Service("token", func() {
	Description("Issues GitHub App installation tokens.")

	Error("bad_request", func() { Description("Invalid request.") })
	Error("unauthorized", func() { Description("Caller is not authenticated.") })
	Error("forbidden", func() { Description("Caller is not allowed to request this token.") })
	Error("not_found", func() { Description("The GitHub App is not installed for the repository.") })
	Error("rate_limited", func() { Description("The caller exceeded the request limit.") })
	Error("upstream", func() { Description("The GitHub API request failed.") })
	Error("timeout", func() { Description("The GitHub API request timed out.") })
	Error("not_ready", func() { Description("The service is not ready to serve requests.") })

	Method("issue", func() {
		Description("Issue a GitHub App installation token.")
		Payload(TokenPayload)
		Result(TokenResult)

		HTTP(func() {
			POST("/v1/token")
			Response(StatusOK)
			Response("bad_request", StatusBadRequest)
			Response("unauthorized", StatusUnauthorized)
			Response("forbidden", StatusForbidden)
			Response("not_found", StatusNotFound)
			Response("rate_limited", StatusTooManyRequests)
			Response("upstream", StatusBadGateway)
			Response("timeout", StatusGatewayTimeout)
		})
	})

	Method("healthz", func() {
		Description("Liveness check.")
		Result(String)

		HTTP(func() {
			GET("/healthz")
			Response(StatusOK)
		})
	})

	Method("readyz", func() {
		Description("Readiness check.")
		Result(String)

		HTTP(func() {
			GET("/readyz")
			Response(StatusOK)
			Response("not_ready", StatusServiceUnavailable)
		})
	})
})
