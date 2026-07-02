package schema

import (
	"time"

	"entgo.io/ent"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
	"entgo.io/ent/schema/mixin"
)

// DevBuild holds the schema definition for the DevBuild entity.
type DevBuild struct {
	ent.Schema
}

// Fields of the DevBuild.
func (DevBuild) Fields() []ent.Field {
	return []ent.Field{}
}

func (DevBuild) Mixin() []ent.Mixin {
	return []ent.Mixin{
		DevBuildMeta{},
		DevBuildSpec{},
		DevBuildStatus{},
	}
}

// Edges of the DevBuild.
func (DevBuild) Edges() []ent.Edge {
	return nil
}

// Annotations of the DevBuild.
func (DevBuild) Annotations() []schema.Annotation {
	return []schema.Annotation{
		entsql.Annotation{Table: "dev_builds_v2"},
	}
}

// DevBuildMeta holds the mixin schema definition.
type DevBuildMeta struct {
	mixin.Schema
}

// Fields of the DevBuildMeta.
func (DevBuildMeta) Fields() []ent.Field {
	return []ent.Field{
		field.String("createdBy").
			Optional().
			MaxLen(64).
			Comment("User who created the build").
			StructTag(`json:"createdBy,omitempty"`),

		field.Time("createdAt").
			Default(time.Now).
			Comment("Time when the build was created").
			StructTag(`json:"createdAt,omitempty"`),

		field.Time("updatedAt").
			Default(time.Now).
			UpdateDefault(time.Now).
			Comment("Time when the build was last updated").
			StructTag(`json:"updatedAt,omitempty"`),
	}
}

// DevBuildSpec holds the mixin schema definition.
type DevBuildSpec struct {
	mixin.Schema
}

// Fields of the DevBuildSpec.
func (DevBuildSpec) Fields() []ent.Field {
	return []ent.Field{
		field.String("product").
			Optional().
			MaxLen(32).
			Comment("Product being built"),

		field.String("edition").
			Optional().
			MaxLen(32).
			Comment("Edition of the product"),

		field.String("version").
			Optional().
			MaxLen(128).
			Comment("Version of the build"),

		// Git related fields
		field.String("githubRepo").
			Optional().
			MaxLen(64).
			Comment("GitHub repository"),

		field.String("gitRef").
			Optional().
			MaxLen(64).
			Comment("Git reference of the build"),

		field.String("gitHash").
			Optional().
			MaxLen(40).
			Comment("Git commit hash"),

		field.String("pluginGitRef").
			Optional().
			MaxLen(64).
			Comment("Git reference of the plugin"),

		// Publish configuration fields
		field.Bool("isHotfix").
			Default(false).
			Comment("Whether the build is a hotfix"),

		field.Bool("isPushGCR").
			Optional().
			Comment("Whether to push to GCR"),

		field.String("targetImg").
			Optional().
			MaxLen(128).
			Comment("Target image name"),

		// Build configuration fields
		field.String("pipelineEngine").
			Optional().
			MaxLen(16).
			Default("tekton").
			Comment("Pipeline engine used"),

		field.String("platform").
			Optional().
			Default("").
			Comment("Build for target platforms"),

		field.String("builderImg").
			Optional().
			MaxLen(128).
			Comment("Builder image used"),

		field.String("buildEnv").
			Optional().
			MaxLen(128).
			Comment("Build environment"),

		field.String("features").
			Optional().
			MaxLen(128).
			Comment("Features included in the build"),

		field.String("productBaseImg").
			Optional().
			MaxLen(128).
			Comment("Base image for the product"),

		field.String("productDockerfile").
			Optional().
			MaxLen(128).
			Comment("Path to artifact image building dockerfile"),
	}
}

// DevBuildStatus holds the mixin schema definition.
type DevBuildStatus struct {
	mixin.Schema
}

// Fields of the DevBuildStatus.
func (DevBuildStatus) Fields() []ent.Field {
	return []ent.Field{
		field.String("status").
			Optional().
			MaxLen(16).
			Default("PENDING").
			Comment("Build status"),

		field.String("errMsg").
			Optional().
			MaxLen(256).
			Comment("Build status message"),

		field.JSON("notificationState", NotificationState{}).
			Optional().
			Comment("Notification delivery state for each channel (Lark DM, group chat, etc.)"),

		field.Int("pipelineBuildID").
			Optional().
			Comment("ID of the pipeline build"),

		field.Time("pipelineStartAt").
			Optional().
			Comment("Build pipeline started time"),

		field.Time("pipelineEndAt").
			Optional().
			Comment("Build pipeline completed time"),

		field.JSON("buildReport", BuildReport{}).
			Optional().
			Comment("JSON report of the build"),

		field.JSON("tektonStatus", TektonStatus{}).
			Optional().
			Comment("Tekton status"),
	}
}
