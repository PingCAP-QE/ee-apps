package design

import . "goa.design/goa/v3/dsl"

var issuerKey = APIKeySecurity("issuer_key", func() {
	Description("Authorization: Bearer <issuer-secret>; repository, tag and TTL policies are independently enforced")
})

var shareArtifact = Type("ShareArtifact", func() {
	Attribute("repository", String, func() { MaxLength(512) })
	Attribute("tag", String, func() { MaxLength(128) })
	Attribute("file", String, func() { MaxLength(255) })
	Required("repository", "tag", "file")
})

var pinnedArtifact = Type("PinnedArtifact", func() {
	Attribute("repository", String)
	Attribute("source_tag", String)
	Attribute("manifest_digest", String)
	Attribute("blob_digest", String)
	Attribute("filename", String)
	Attribute("size", Int64)
	Attribute("media_type", String)
	Required("repository", "source_tag", "manifest_digest", "blob_digest", "filename", "size", "media_type")
})

var shareResult = Type("Share", func() {
	Attribute("id", String)
	Attribute("download_url", String, "Returned only once, at creation")
	Attribute("artifact", pinnedArtifact)
	Attribute("issued_by", String)
	Attribute("customer_id", String)
	Attribute("purpose", String)
	Attribute("created_at", String)
	Attribute("expires_at", String)
	Attribute("revoked_at", String)
	Attribute("max_sessions", Int)
	Attribute("sessions_started", Int)
	Required("id", "artifact", "issued_by", "purpose", "created_at", "expires_at", "sessions_started")
})

var _ = Service("shares", func() {
	Description("Trusted delivery grant management; mount only in management mode")
	Security(issuerKey)
	Error("unauthorized", ErrorResult)
	Error("forbidden", ErrorResult)
	Error("invalid_request", ErrorResult)
	Error("not_found", ErrorResult)
	Error("unavailable", ErrorResult)
	HTTP(func() {
		Response("unauthorized", StatusUnauthorized)
		Response("forbidden", StatusForbidden)
		Response("invalid_request", StatusBadRequest)
		Response("not_found", StatusNotFound)
		Response("unavailable", StatusServiceUnavailable)
	})
	Method("create", func() {
		Payload(func() {
			APIKey("issuer_key", "authorization", String)
			Attribute("artifact", shareArtifact)
			Attribute("expires_in_seconds", Int64, func() { Minimum(1); Maximum(30 * 86400); Example(86400) })
			Attribute("max_sessions", Int, "GET authorizations; HEAD never consumes a session", func() { Minimum(1); Maximum(2147483647); Example(5) })
			Attribute("purpose", String, func() { Enum("customer-delivery") })
			Attribute("customer_id", String, func() { MaxLength(255) })
			Required("authorization", "artifact", "purpose")
		})
		Result(shareResult)
		HTTP(func() {
			POST("/api/v1/shares")
			Header("authorization:Authorization")
			Response(StatusCreated)
		})
	})
	Method("show", func() {
		Payload(func() {
			APIKey("issuer_key", "authorization", String)
			Attribute("id", String, func() { Format(FormatUUID) })
			Required("authorization", "id")
		})
		Result(shareResult)
		HTTP(func() { GET("/api/v1/shares/{id}"); Header("authorization:Authorization") })
	})
	Method("revoke", func() {
		Payload(func() {
			APIKey("issuer_key", "authorization", String)
			Attribute("id", String, func() { Format(FormatUUID) })
			Required("authorization", "id")
		})
		HTTP(func() {
			DELETE("/api/v1/shares/{id}")
			Header("authorization:Authorization")
			Response(StatusNoContent)
		})
	})
})
