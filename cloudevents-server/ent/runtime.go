// Code generated by ent, DO NOT EDIT.

package ent

import (
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent/problemcaserun"
	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent/schema"
)

// The init function reads all schema descriptors with runtime code
// (default values, validators, hooks and policies) and stitches it
// to their package variables.
func init() {
	problemcaserunFields := schema.ProblemCaseRun{}.Fields()
	_ = problemcaserunFields
	// problemcaserunDescFlaky is the schema descriptor for flaky field.
	problemcaserunDescFlaky := problemcaserunFields[4].Descriptor()
	// problemcaserun.DefaultFlaky holds the default value on creation for the flaky field.
	problemcaserun.DefaultFlaky = problemcaserunDescFlaky.Default.(bool)
	// problemcaserunDescBuildURL is the schema descriptor for build_url field.
	problemcaserunDescBuildURL := problemcaserunFields[7].Descriptor()
	// problemcaserun.BuildURLValidator is a validator for the "build_url" field. It is called by the builders before save.
	problemcaserun.BuildURLValidator = problemcaserunDescBuildURL.Validators[0].(func(string) error)
}
