package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/schema/field"
)

// AlertEvent records the last time a global alert condition was observed by any
// worker. It exists because the leader is the only sender: a condition detected
// by a follower (for example the reaper reclaiming an expired lease) has to be
// visible to the leader in the next tick.
type AlertEvent struct {
	ent.Schema
}

// Fields of the AlertEvent.
func (AlertEvent) Fields() []ent.Field {
	return []ent.Field{
		field.String("name").
			MaxLen(64).
			Unique().
			Comment("Event kind, for example lease-reclaim"),

		field.Time("lastAt").
			Comment("Time when the condition last happened"),

		field.Int("count").
			Default(0).
			Comment("Total occurrences recorded for this event"),

		field.Time("updatedAt").
			Default(time.Now).
			UpdateDefault(time.Now).
			Comment("Time when the row was last updated"),
	}
}
