package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/dialect"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/edge"
	"entgo.io/ent/schema/field"
	"entgo.io/ent/schema/index"
)

// RequestFile is one artifact target inside a request. The share ID and the
// encrypted, one-time download URL are filled in after approval.
type RequestFile struct {
	ent.Schema
}

func (RequestFile) Fields() []ent.Field {
	return []ent.Field{
		field.String("id").
			MaxLen(36).
			Immutable(),
		field.String("request_id").
			MaxLen(36),
		field.String("repository").
			MaxLen(512).
			Immutable(),
		field.String("tag").
			MaxLen(128).
			Immutable(),
		field.String("filename").
			MaxLen(255).
			Immutable(),
		field.String("share_id").
			MaxLen(36).
			Default(""),
		field.Bytes("download_url_ciphertext").
			Optional().
			Sensitive(),
		field.Time("delivered_at").
			Optional().
			Nillable().
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}),
		field.Time("created_at").
			Default(time.Now).
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}).
			Immutable(),
	}
}

func (RequestFile) Edges() []ent.Edge {
	return []ent.Edge{
		edge.From("request", Request.Type).
			Ref("files").
			Unique().
			Required().
			Field("request_id"),
	}
}

func (RequestFile) Indexes() []ent.Index {
	return []ent.Index{
		index.Fields("request_id"),
		index.Fields("request_id", "repository", "tag", "filename").Unique(),
	}
}

func (RequestFile) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "request_files"},
	}
}
