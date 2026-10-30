package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/schema/field"
)

// AlertLease is the global alert-evaluation leader lease. Every worker replica
// and every cluster shares the same row, so only one of them evaluates alerts
// and calls the notifier at a time.
type AlertLease struct {
	ent.Schema
}

// Fields of the AlertLease.
func (AlertLease) Fields() []ent.Field {
	return []ent.Field{
		field.String("name").
			MaxLen(64).
			Unique().
			Comment("Lease name, for example publish-alerts"),

		field.String("holder").
			MaxLen(128).
			Comment("Worker identity currently holding the lease"),

		field.Time("leaseUntil").
			Comment("Lease expiry; an expired lease may be taken over by another worker"),

		field.Time("updatedAt").
			Default(time.Now).
			UpdateDefault(time.Now).
			Comment("Time when the lease was last refreshed"),
	}
}
