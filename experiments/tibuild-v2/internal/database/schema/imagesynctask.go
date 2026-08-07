package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
)

// ImageSyncTask holds the schema definition for the ImageSyncTask entity.
// It records an asynchronous image copy (sync) task.
type ImageSyncTask struct {
	ent.Schema
}

// Fields of the ImageSyncTask.
func (ImageSyncTask) Fields() []ent.Field {
	return []ent.Field{
		field.String("source").
			MaxLen(256).
			Comment("Source image reference"),

		field.String("target").
			MaxLen(256).
			Comment("Target image reference"),

		field.String("status").
			Default("PENDING").
			MaxLen(16).
			Comment("Task status: PENDING, PROCESSING, SUCCEEDED, FAILED"),

		field.String("errMsg").
			Optional().
			MaxLen(512).
			Comment("Error message when the task failed"),

		field.Int("retryCount").
			Default(0).
			Comment("Number of times the task has been retried"),

		field.Time("createdAt").
			Default(time.Now).
			Comment("Time when the task was created"),

		field.Time("updatedAt").
			Default(time.Now).
			UpdateDefault(time.Now).
			Comment("Time when the task was last updated"),
	}
}

// Edges of the ImageSyncTask.
func (ImageSyncTask) Edges() []ent.Edge {
	return nil
}

// Annotations of the ImageSyncTask.
func (ImageSyncTask) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "image_sync_tasks"},
	}
}
