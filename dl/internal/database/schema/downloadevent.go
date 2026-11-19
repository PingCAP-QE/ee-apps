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

// DownloadEvent is a deliberately token-free delivery audit record.
type DownloadEvent struct {
	ent.Schema
}

func (DownloadEvent) Fields() []ent.Field {
	return []ent.Field{
		field.Int64("id"),
		field.String("share_id").
			MaxLen(36).
			Optional().
			Nillable(),
		field.String("phase").
			MaxLen(32),
		field.String("outcome").
			MaxLen(32),
		field.Int64("bytes_transferred").
			Default(0).
			NonNegative(),
		field.Time("created_at").
			Default(time.Now).
			SchemaType(map[string]string{dialect.MySQL: "datetime(6)"}).
			Annotations(entsql.Annotation{DefaultExpr: "CURRENT_TIMESTAMP(6)"}).
			Immutable(),
	}
}

func (DownloadEvent) Indexes() []ent.Index {
	return []ent.Index{
		index.Fields("created_at").StorageKey("download_events_created"),
		index.Fields("share_id").StorageKey("download_events_share"),
	}
}

func (DownloadEvent) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "download_events"},
	}
}
