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
		entsql.Annotation{Table: "dev_builds"},
	}
}

// DevBuildMeta holds the mixin schema definition.
type DevBuildMeta struct {
	mixin.Schema
}

// Fields of the DevBuildMeta.
func (DevBuildMeta) Fields() []ent.Field {
	return []ent.Field{
		field.String("created_by").
			Optional().
			MaxLen(64).
			Comment("User who created the build").
			StructTag(`json:"created_by,omitempty"`),

		field.Time("created_at").
			Default(time.Now).
			Comment("Time when the build was created").
			StructTag(`json:"created_at,omitempty"`),

		field.Time("updated_at").
			Default(time.Now).
			UpdateDefault(time.Now).
			Comment("Time when the build was last updated").
			StructTag(`json:"updated_at,omitempty"`),
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
		field.String("github_repo").
			Optional().
			MaxLen(64).
			Comment("GitHub repository"),

		field.String("git_ref").
			Optional().
			MaxLen(64).
			Comment("Git reference of the build"),

		field.String("git_hash").
			Optional().
			MaxLen(40).
			Comment("Git commit SHA"),

		field.String("plugin_git_ref").
			Optional().
			MaxLen(64).
			Comment("Git reference of the plugin"),

		// Publish configuration fields
		field.Bool("is_hotfix").
			Default(false).
			Comment("Whether the build is a hotfix"),

		field.Bool("is_push_gcr").
			Optional().
			Comment("Whether to push to GCR"),

		field.String("target_img").
			Optional().
			MaxLen(128).
			Comment("Target image name"),

		// Build configuration fields
		field.String("pipeline_engine").
			Optional().
			MaxLen(16).
			Default("jenkins").
			Comment("Pipeline engine used"),

		field.String("builder_img").
			Optional().
			MaxLen(128).
			Comment("Builder image used"),

		field.String("build_env").
			Optional().
			MaxLen(128).
			Comment("Build environment"),

		field.String("features").
			Optional().
			MaxLen(128).
			Comment("Features included in the build"),

		field.String("product_base_img").
			Optional().
			MaxLen(128).
			Comment("Base image for the product"),

		field.String("product_dockerfile").
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
			Default("pending").
			Comment("Build status"),

		field.String("err_msg").
			Optional().
			MaxLen(256).
			Comment("Build status message"),

		field.Int64("pipeline_build_id").
			Optional().
			Comment("ID of the pipeline build"),

		field.Time("pipeline_start_at").
			Optional().
			Comment("Build pipeline started time"),

		field.Time("pipeline_end_at").
			Optional().
			Comment("Build pipeline completed time"),

		field.JSON("build_report", map[string]any{}).
			Optional().
			Comment("JSON report of the build"),

		field.JSON("tekton_status", map[string]any{}).
			Optional().
			Comment("Tekton status"),
	}
}
