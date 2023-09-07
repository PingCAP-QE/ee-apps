package testcaserun

// ProblemCasesFromBazel present case run records from bazel.
type ProblemCasesFromBazel struct {
	NewFlaky []string           `yaml:"new_flaky,omitempty" json:"new_flaky,omitempty"`
	LongTime map[string]float64 `yaml:"long_time,omitempty" json:"long_time,omitempty"`
}
