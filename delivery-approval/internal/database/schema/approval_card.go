package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/dialect"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
	"entgo.io/ent/schema/index"
)

// ApprovalCard remembers one Lark card that announced a request, so the same
// card can be patched to its decided state after approval, rejection, revoke or
// expiry. It only stores identifiers: no token and no message body.
type ApprovalCard struct {
	ent.Schema
}

func (ApprovalCard) Fields() []ent.Field {
	return []ent.Field{
		field.String("id").
			MaxLen(36).
			Immutable(),
		field.String("request_id").
			MaxLen(36),
		// Empty for a card sent to the shared approval group.
		field.String("approver_email").
			MaxLen(320).
			Default("").
			Immutable(),
		field.String("message_id").
			MaxLen(128).
			Immutable(),
		field.Time("created_at").
			Default(time.Now).
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}).
			Immutable(),
	}
}

func (ApprovalCard) Indexes() []ent.Index {
	return []ent.Index{
		index.Fields("request_id"),
		index.Fields("message_id").Unique(),
	}
}

func (ApprovalCard) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "approval_cards"},
	}
}
