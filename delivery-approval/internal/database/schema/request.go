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

// Request is one delivery request. It is created by the requester, decided by
// an allowlisted approver, and then either delivered or revoked/expired.
type Request struct {
	ent.Schema
}

func (Request) Fields() []ent.Field {
	return []ent.Field{
		field.String("id").
			MaxLen(36).
			Immutable(),
		field.String("requester_email").
			MaxLen(320).
			Immutable(),
		field.String("requester_sub").
			MaxLen(255).
			Immutable(),
		field.String("purpose").
			MaxLen(64).
			Immutable(),
		field.String("note").
			MaxLen(1024).
			Default("").
			Immutable(),
		field.Enum("status").
			Values("pending", "approved", "rejected", "revoked", "expired").
			Default("pending"),
		field.Int64("ttl_seconds").
			Positive().
			Immutable(),
		field.Time("created_at").
			Default(time.Now).
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}).
			Immutable(),
		field.Time("expires_at").
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}).
			Immutable(),
		field.Time("decided_at").
			Optional().
			Nillable().
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}),
		field.String("approver_email").
			MaxLen(320).
			Default(""),
		field.String("decision_note").
			MaxLen(1024).
			Default(""),
		field.Enum("delivery_status").
			Values("pending", "delivering", "delivered", "failed").
			Default("pending"),
		field.Int("delivery_attempts").
			Default(0).
			NonNegative(),
		field.String("last_error_class").
			MaxLen(64).
			Default(""),
	}
}

func (Request) Edges() []ent.Edge {
	return []ent.Edge{
		edge.To("files", RequestFile.Type).
			Annotations(entsql.OnDelete(entsql.Cascade)),
		edge.To("events", RequestEvent.Type).
			Annotations(entsql.OnDelete(entsql.Cascade)),
	}
}

func (Request) Indexes() []ent.Index {
	return []ent.Index{
		index.Fields("status"),
		index.Fields("requester_email"),
		index.Fields("expires_at"),
	}
}

func (Request) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{
			Table: "requests",
			Checks: map[string]string{
				"requests_expiry_after_creation": "expires_at > created_at",
				"requests_delivery_nonnegative":  "delivery_attempts >= 0",
				"requests_ttl_positive":          "ttl_seconds > 0",
			},
		},
	}
}
