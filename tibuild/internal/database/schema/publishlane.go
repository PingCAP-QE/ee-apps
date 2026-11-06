package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
	"entgo.io/ent/schema/index"
)

// PublishLane holds the schema definition for the PublishLane entity.
//
// A lane is a single-row semaphore: it serializes publication intents that
// must not overlap. The TiUP kind needs exactly one lane (`lane=tiup`) because
// two mirrors must never be published at the same time, and because a unit's
// steps must not interleave with another unit's steps.
//
// A lane is owned by a unit, not by a process: the worker that finishes the
// prod step releases the claim by making the staging step claimable, and the
// worker that picks it up renews the same lane row. The lease makes the lane
// recoverable when a worker dies.
type PublishLane struct {
	ent.Schema
}

// Fields of the PublishLane.
func (PublishLane) Fields() []ent.Field {
	return []ent.Field{
		field.Int("id").
			Immutable().
			Comment("Fixed row id; a lane table holds exactly one row per lane"),

		field.String("lane").
			MaxLen(32).
			Immutable().
			Comment("Lane name; the TiUP publisher uses 'tiup'"),

		field.String("unit_id").
			MaxLen(36).
			Optional().
			Comment("Unit currently holding the lane; empty when idle"),

		field.String("holder").
			MaxLen(128).
			Optional().
			Comment("Worker that last renewed the lease"),

		field.Time("lease_until").
			Optional().
			Nillable().
			Comment("Lease expiry; an expired lane can be acquired by another unit"),

		field.Time("created_at").
			Default(time.Now).
			Comment("Time when the lane row was created"),

		field.Time("updated_at").
			Default(time.Now).
			UpdateDefault(time.Now).
			Comment("Time of the last acquire/renew/release"),
	}
}

// Indexes of the PublishLane.
func (PublishLane) Indexes() []ent.Index {
	return []ent.Index{
		// One row per lane name keeps a buggy seed from creating a second
		// semaphore that would silently break mutual exclusion.
		index.Fields("lane").
			Unique().
			StorageKey("uk_lane"),
	}
}

// Annotations of the PublishLane.
func (PublishLane) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "publish_lane"},
	}
}
