package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
	"entgo.io/ent/schema/index"
)

// PublishDesired holds the schema definition for the PublishDesired entity.
//
// A row is the desired state of one TiUP package channel: the newest artifact
// pushed for (target, pkg, os, arch). A nightly request upserts the desired
// fields and never drops the request, so the row is the "latest wins" inbox;
// `next_run_at` makes it the schedule, and a tick publishes the row once per
// window.
//
// `running_unit_id` is the conditional claim. It is empty while the row is
// idle; a tick only publishes an idle row, and a request that arrives while a
// unit is in flight updates only the desired fields, so the running unit's
// parameters are never torn and the next tick publishes the newest artifact.
//
// The logical identity is (target, pkg, os, arch). Ent has no composite
// primary keys, so the table keeps an auto-increment id and enforces the
// identity with a unique index; MySQL and SQLite both reject a duplicate key,
// which is what the upsert and the conditional claim rely on.
type PublishDesired struct {
	ent.Schema
}

// Fields of the PublishDesired.
func (PublishDesired) Fields() []ent.Field {
	return []ent.Field{
		field.String("target").
			MaxLen(32).
			Comment("Publish target: tiup-staging or tiup-prod"),

		field.String("pkg").
			MaxLen(64).
			Comment("TiUP package name, for example tidb or pd"),

		field.String("os").
			MaxLen(16).
			Comment("Package operating system: linux or darwin"),

		field.String("arch").
			MaxLen(16).
			Comment("Package architecture: amd64 or arm64"),

		field.String("desired_version").
			MaxLen(64).
			Comment("Version of the newest artifact pushed in the window"),

		field.JSON("desired_payload", map[string]any{}).
			Comment("Full execution parameters of the newest artifact"),

		field.Time("desired_at").
			Comment("When the newest artifact was pushed"),

		field.Time("next_run_at").
			Comment("Earliest time the row may be published; aligned to the window anchor"),

		field.String("running_unit_id").
			MaxLen(36).
			Optional().
			Comment("Unit currently publishing this row; empty when idle"),

		field.String("last_succeeded_version").
			MaxLen(64).
			Optional().
			Comment("Version of the last successful publication"),

		field.String("last_failed_version").
			MaxLen(64).
			Optional().
			Comment("Version of the last failed publication"),

		field.Text("last_error").
			Optional().
			Comment("Last failure message"),

		field.Time("created_at").
			Default(time.Now).
			Comment("Time when the row was first created"),

		field.Time("updated_at").
			Default(time.Now).
			UpdateDefault(time.Now).
			Comment("Time of the last upsert, claim or completion"),
	}
}

// Indexes of the PublishDesired.
func (PublishDesired) Indexes() []ent.Index {
	return []ent.Index{
		// The logical primary key: one desired row per channel.
		index.Fields("target", "pkg", "os", "arch").
			Unique().
			StorageKey("uk_desired_key"),
		// Serves the tick query: due rows for the targets this worker owns.
		index.Fields("target", "next_run_at").
			StorageKey("idx_desired_due"),
	}
}

// Annotations of the PublishDesired.
func (PublishDesired) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "publish_desired"},
	}
}
