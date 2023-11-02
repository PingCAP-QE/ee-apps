package testcaserun

const (
	reasonNotFinished = "not_finished"
	reasonUnknown     = "unknown"
)

// ProblemCasesFromBazel present case run records from bazel.
type ProblemCasesFromBazel struct {
	NewFlaky []flaky            `yaml:"new_flaky,omitempty" json:"new_flaky,omitempty"`
	LongTime map[string]float64 `yaml:"long_time,omitempty" json:"long_time,omitempty"`
}

type flaky struct {
	Name   string `yaml:"name,omitempty" json:"name,omitempty"`
	Reason string `yaml:"reason,omitempty" json:"reason,omitempty"`
}
