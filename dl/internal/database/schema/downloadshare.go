package schema

import (
	"entgo.io/ent"
	"entgo.io/ent/dialect"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
	"entgo.io/ent/schema/index"
)

// DownloadShare is an expiring, revocable capability for one pinned OCI blob.
type DownloadShare struct {
	ent.Schema
}

func (DownloadShare) Fields() []ent.Field {
	return []ent.Field{
		field.String("id").
			MaxLen(36).
			Immutable(),
		field.Bytes("token_hash").
			SchemaType(map[string]string{dialect.MySQL: "binary(32)"}).
			Immutable().
			Unique(),
		field.String("repository").
			MaxLen(512).
			Immutable(),
		field.String("source_tag").
			MaxLen(128).
			Immutable(),
		field.String("manifest_digest").
			MaxLen(128).
			Immutable(),
		field.String("blob_digest").
			MaxLen(128).
			Immutable(),
		field.Int64("blob_size").
			NonNegative().
			Immutable(),
		field.String("media_type").
			MaxLen(255).
			Immutable(),
		field.String("filename").
			MaxLen(255).
			Immutable(),
		field.String("customer_id").
			MaxLen(255).
			Immutable(),
		field.String("purpose").
			MaxLen(64).
			Immutable(),
		field.String("issued_by").
			MaxLen(128).
			Immutable(),
		field.Time("created_at").
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}).
			Immutable(),
		field.Time("expires_at").
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}).
			Immutable(),
		field.Time("revoked_at").
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}).
			Optional().
			Nillable(),
		field.Int("max_sessions").
			Positive().
			Optional().
			Nillable().
			Immutable(),
		field.Int64("sessions_started").
			Default(0).
			NonNegative(),
	}
}

func (DownloadShare) Indexes() []ent.Index {
	return []ent.Index{
		index.Fields("expires_at").StorageKey("download_shares_expiry"),
		index.Fields("issued_by").StorageKey("download_shares_issuer"),
	}
}

func (DownloadShare) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{
			Table: "download_shares",
			Checks: map[string]string{
				"download_shares_expiry_after_creation": "expires_at > created_at",
				"download_shares_blob_nonnegative":      "blob_size >= 0",
				"download_shares_max_sessions_positive": "max_sessions IS NULL OR max_sessions > 0",
				"download_shares_sessions_nonnegative":  "sessions_started >= 0",
			},
		},
	}
}
