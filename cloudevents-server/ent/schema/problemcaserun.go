package schema

import (
	"entgo.io/ent"
	"entgo.io/ent/schema/field"
)

// ProblemCaseRun holds the schema definition for the ProblemCaseRun entity.
type ProblemCaseRun struct {
	ent.Schema
}

// Fields of the ProblemCaseRun.
func (ProblemCaseRun) Fields() []ent.Field {
	return []ent.Field{
		field.String("repo").Comment("repo full name"),
		field.String("branch").Comment("base branch"),
		field.String("suite_name").Comment("suite name, target name in bazel."),
		field.String("case_name").Comment("case name, may be TextXxx.TestYyy format."),
		field.Bool("flaky").Default(false).Comment("is it a flay run?"),
		field.Int("timecost_ms").Comment("timecost(milliseconds) of the test case run"),
		field.Time("report_time").Comment("report unit timestamp"),
		field.String("build_url").Comment("CI build url"),
	}
}

// Edges of the ProblemCaseRun.
func (ProblemCaseRun) Edges() []ent.Edge {
	return nil
}
