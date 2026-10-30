package delivery_test

import (
	"os"
	"sort"
	"testing"

	"github.com/stretchr/testify/require"
	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// tiupGolden is the corpus recorded from the pre-migration publisher
// (publisher/internal/service/impl/tiup, build tag `golden`). It embeds both the
// rules and the OCI artifact config, so the test needs no network and no
// sibling checkout.
type tiupGolden struct {
	GeneratedAt          string                         `yaml:"generated_at"`
	DeliveryConfigSHA256 string                         `yaml:"delivery_config_sha256"`
	Rules                map[string][]delivery.TiUPRule `yaml:"rules"`
	Cases                []tiupGoldenCase               `yaml:"cases"`
}

type tiupGoldenCase struct {
	ArtifactURL    string         `yaml:"artifact_url"`
	ArtifactConfig map[string]any `yaml:"artifact_config"`
	Steps          []tiupStep     `yaml:"steps"`
}

type tiupStep struct {
	Target      string `yaml:"target"`
	Name        string `yaml:"name"`
	Version     string `yaml:"version"`
	OS          string `yaml:"os"`
	Arch        string `yaml:"arch"`
	Description string `yaml:"description,omitempty"`
	EntryPoint  string `yaml:"entry_point"`
	Standalone  bool   `yaml:"standalone"`
	File        string `yaml:"file"`
	Nightly     bool   `yaml:"nightly"`
}

func loadTiUPGolden(t *testing.T) *tiupGolden {
	t.Helper()
	content, err := os.ReadFile("testdata/tiup_delivery_golden.yaml")
	require.NoError(t, err)

	var doc tiupGolden
	require.NoError(t, yaml.Unmarshal(content, &doc))
	require.NotEmpty(t, doc.Rules)
	require.NotEmpty(t, doc.Cases)
	return &doc
}

// TestPlanTiUPMatchesLegacyPublisher expands every recorded artifact and
// compares the plan field by field with what the old publisher produced.
//
// The only documented difference is ordering inside a target, which the golden
// file normalises by sorting on (target, name).
func TestPlanTiUPMatchesLegacyPublisher(t *testing.T) {
	doc := loadTiUPGolden(t)
	cfg := &delivery.Config{TiUPPublishRules: doc.Rules}

	var compared int
	for _, tc := range doc.Cases {
		plans, err := cfg.PlanTiUP(tc.ArtifactURL, tc.ArtifactConfig)
		require.NoError(t, err, tc.ArtifactURL)

		require.Equal(t, expectedPlans(tc.Steps), normalisePlans(plans), "plan mismatch for %s", tc.ArtifactURL)
		compared++
	}
	require.Equal(t, len(doc.Cases), compared)
}

// The rules snapshot must be the one the golden was recorded from, otherwise
// the comparison above proves nothing.
func TestTiUPGoldenRulesSnapshotIsStable(t *testing.T) {
	doc := loadTiUPGolden(t)
	require.NotEmpty(t, doc.GeneratedAt)
	require.Len(t, doc.DeliveryConfigSHA256, 64, "the recording must pin the delivery.yaml revision")
	require.Contains(t, doc.Rules, "^us-docker.pkg.dev/pingcap-testing-account/hub/.+/package$")
	require.Contains(t, doc.Rules, "^us-docker.pkg.dev/pingcap-testing-account/tidbx/.+/package$")
}

// expectedPlans rebuilds the plan shape the recorder wrote: steps are grouped
// by target and sorted by package name.
func expectedPlans(steps []tiupStep) []delivery.TiUPPlan {
	byTarget := map[string]*delivery.TiUPPlan{}
	var order []string
	for _, step := range steps {
		// The recorder logged the mirror name the old worker published to; the
		// new plan keys the step on the worker shard (`tiup-<mirror>`).
		target := "tiup-" + step.Target
		plan, ok := byTarget[target]
		if !ok {
			plan = &delivery.TiUPPlan{
				Target:  target,
				Mirror:  step.Target,
				Version: step.Version,
				Nightly: step.Nightly,
			}
			byTarget[target] = plan
			order = append(order, target)
		}
		plan.Packages = append(plan.Packages, delivery.TiUPPackage{
			Name:        step.Name,
			Version:     step.Version,
			OS:          step.OS,
			Arch:        step.Arch,
			Description: step.Description,
			EntryPoint:  step.EntryPoint,
			File:        step.File,
			Standalone:  step.Standalone,
		})
	}
	sort.Strings(order)

	plans := make([]delivery.TiUPPlan, 0, len(order))
	for _, target := range order {
		plan := byTarget[target]
		sort.Slice(plan.Packages, func(i, j int) bool { return plan.Packages[i].Name < plan.Packages[j].Name })
		plans = append(plans, *plan)
	}
	return plans
}

func normalisePlans(plans []delivery.TiUPPlan) []delivery.TiUPPlan {
	out := make([]delivery.TiUPPlan, 0, len(plans))
	for _, plan := range plans {
		packages := append([]delivery.TiUPPackage(nil), plan.Packages...)
		sort.Slice(packages, func(i, j int) bool { return packages[i].Name < packages[j].Name })
		plan.Packages = packages
		out = append(out, plan)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Target < out[j].Target })
	return out
}
