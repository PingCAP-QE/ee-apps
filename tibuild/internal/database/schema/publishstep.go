package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
	"entgo.io/ent/schema/index"
)

// PublishStep holds the schema definition for the PublishStep entity.
//
// A step is one destination of a unit and the unit of work a worker claims.
// `target` is the registry host (or the mirror name for TiUP) a worker declares
// credentials and network reachability for; `dest` is the destination
// repository (empty for kinds whose target is already unique inside a unit).
type PublishStep struct {
	ent.Schema
}

// Fields of the PublishStep.
func (PublishStep) Fields() []ent.Field {
	return []ent.Field{
		field.String("id").
			MaxLen(36).
			Immutable().
			Comment("UUID of the step"),

		field.String("unit_id").
			MaxLen(36).
			Immutable().
			Comment("Owning publish_unit id"),

		field.String("target").
			MaxLen(64).
			Comment("Claim shard: registry host for image_copy, mirror name for tiup"),

		field.String("dest").
			MaxLen(256).
			Default("").
			Comment("Destination repository inside the target"),

		field.String("depends_on").
			MaxLen(36).
			Optional().
			Comment("Previous step of the same unit; image steps never depend on each other"),

		field.String("state").
			MaxLen(16).
			Comment("Step state: blocked, pending, claimed, succeeded, failed, skipped"),

		field.Int("priority").
			Default(PublishPriorityAPI).
			Comment("Claim priority copied from the owning unit: release > api > nightly"),

		field.JSON("payload", map[string]any{}).
			Comment("Full execution parameters, replayable by any worker"),

		field.JSON("result", map[string]any{}).
			Optional().
			Comment("Execution outcome recorded by the worker"),

		field.Int("attempts").
			Default(0).
			Comment("Claim attempts, incremented atomically on claim"),

		field.String("claimed_by").
			MaxLen(128).
			Optional().
			Comment("Worker identity currently holding the lease"),

		field.Time("lease_until").
			Optional().
			Nillable().
			Comment("Lease expiry; an expired claimed row is reclaimed by the reaper"),

		field.Time("available_at").
			Optional().
			Nillable().
			Comment("Earliest time the row may be claimed again after a failure"),

		field.Text("last_error").
			Optional().
			Comment("Last failure message"),

		field.Time("created_at").
			Default(time.Now).
			Comment("Time when the step was created"),

		field.Time("started_at").
			Optional().
			Nillable().
			Comment("Time of the first claim"),

		field.Time("finished_at").
			Optional().
			Nillable().
			Comment("Time when the step reached a terminal state"),
	}
}

// Indexes of the PublishStep.
func (PublishStep) Indexes() []ent.Index {
	return []ent.Index{
		// Image units copy the same source digest into several repositories that
		// can share a registry host (docker.io/pingcap/ticdc and
		// docker.io/pingcap/ticdc-enterprise), so the step identity includes the
		// destination repository.
		index.Fields("unit_id", "target", "dest").
			Unique().
			StorageKey("uk_unit_target_dest"),
		// SQLite keeps index names per database, MySQL per table; prefix the
		// claim index so the two publish tables cannot collide with the image
		// sync queue in the sqlite-backed tests.
		index.Fields("target", "state", "priority", "available_at", "id").
			StorageKey("idx_publish_claim"),
		index.Fields("unit_id").
			StorageKey("idx_unit"),
		index.Fields("depends_on").
			StorageKey("idx_dep"),
	}
}

// Annotations of the PublishStep.
func (PublishStep) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "publish_step"},
	}
}
