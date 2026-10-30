package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
	"entgo.io/ent/schema/index"
)

// PublishUnit holds the schema definition for the PublishUnit entity.
//
// A unit is one publication intent: an artifact published to a set of targets.
// Its steps carry the per-target work. For image copies (`kind=image_copy`) the
// source digest is pinned on the unit, so every step copies exactly the same
// bytes even if the source tag moves while the unit is in flight.
type PublishUnit struct {
	ent.Schema
}

// Fields of the PublishUnit.
func (PublishUnit) Fields() []ent.Field {
	return []ent.Field{
		field.String("id").
			MaxLen(36).
			Immutable().
			Comment("UUID of the unit"),

		field.String("kind").
			MaxLen(16).
			Comment("Publication kind: image_copy (tiup is added by the publish track)"),

		field.String("artifact_url").
			MaxLen(512).
			Comment("Source artifact reference as requested by the caller"),

		field.String("digest").
			MaxLen(128).
			Optional().
			Comment("Resolved source digest, pinned once at planning time"),

		field.String("state").
			MaxLen(16).
			Comment("Unit state: queued, running, succeeded, partial, failed, canceled"),

		field.String("requested_by").
			MaxLen(128).
			Optional().
			Comment("Caller identity, when known"),

		field.Time("created_at").
			Default(time.Now).
			Comment("Time when the unit was created"),

		field.Time("finished_at").
			Optional().
			Nillable().
			Comment("Time when every step reached a terminal state"),
	}
}

// Indexes of the PublishUnit.
func (PublishUnit) Indexes() []ent.Index {
	return []ent.Index{
		index.Fields("state", "created_at").
			StorageKey("idx_unit_state"),
		index.Fields("artifact_url").
			StorageKey("idx_unit_artifact"),
	}
}

// Annotations of the PublishUnit.
func (PublishUnit) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "publish_unit"},
	}
}
