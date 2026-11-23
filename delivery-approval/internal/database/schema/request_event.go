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

// RequestEvent is the append-only audit trail. It never contains tokens or
// plaintext download URLs.
type RequestEvent struct {
	ent.Schema
}

func (RequestEvent) Fields() []ent.Field {
	return []ent.Field{
		field.Int64("id"),
		field.String("request_id").
			MaxLen(36),
		field.String("file_id").
			MaxLen(36).
			Default("").
			Immutable(),
		field.String("actor_email").
			MaxLen(320).
			Default("").
			Immutable(),
		field.String("action").
			MaxLen(32).
			Immutable(),
		field.String("outcome").
			MaxLen(16).
			Immutable(),
		field.String("error_class").
			MaxLen(64).
			Default("").
			Immutable(),
		field.Time("created_at").
			Default(time.Now).
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}).
			Annotations(entsql.Annotation{DefaultExpr: "CURRENT_TIMESTAMP(6)"}).
			Immutable(),
	}
}

func (RequestEvent) Edges() []ent.Edge {
	return []ent.Edge{
		edge.From("request", Request.Type).
			Ref("events").
			Unique().
			Required().
			Field("request_id"),
	}
}

func (RequestEvent) Indexes() []ent.Index {
	return []ent.Index{
		index.Fields("request_id"),
		index.Fields("created_at"),
	}
}

func (RequestEvent) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "request_events"},
	}
}
