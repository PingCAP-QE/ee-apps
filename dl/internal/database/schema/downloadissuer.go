package schema

import (
	"entgo.io/ent"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
)

// DownloadIssuer is an identity that may create and manage delivery grants.
type DownloadIssuer struct {
	ent.Schema
}

func (DownloadIssuer) Fields() []ent.Field {
	return []ent.Field{
		field.String("id").
			MaxLen(128).
			Immutable(),
		field.Bool("enabled").
			Default(false),
	}
}

func (DownloadIssuer) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "download_issuers"},
	}
}
