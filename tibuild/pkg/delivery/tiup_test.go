package delivery_test

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

const tiupTestRules = `
tiup_publish_rules:
  ^example.com/hub/.+/package$:
    - description: nightly
      tags_regex:
        - ^(master|main)_(linux|darwin)_(amd64|arm64)$
      nightly: true
      dest_mirrors: [staging, prod]
    - description: pre branch
      tags_regex:
        - ^(release-[0-9]+[.][0-9]+)(-beta[.][0-9]+)?_(linux|darwin)_(amd64|arm64)$
      dest_mirrors: [staging]
    - description: ga
      tags_regex:
        - ^(v[0-9][.][0-9]+[.][0-9]+(-(beta|rc|release)[.][0-9]+)?)_(linux|darwin)_(amd64|arm64)$
      version_regex_replace: "$1"
      dest_mirrors: [staging, prod]
    - description: rust node
      tags_regex:
        - ^(hparser-integration-rust)_(linux)_(amd64|arm64)$
      version_regex_replace: "v9.0.0-beta.2.pre.rust"
      dest_mirrors: [staging]
  ^example.com/tidbx/.+/package$:
    - description: nextgen
      tags_regex:
        - ^(v[0-9]+[.][0-9]+[.][0-9]+-nextgen[.][0-9]+[.][0-9]+)_(linux|darwin)_(amd64|arm64)$
      version_regex_replace: "$1"
      dest_mirrors: [staging]
`

func tiupTestConfig(t *testing.T) *delivery.Config {
	t.Helper()
	cfg, err := delivery.Parse([]byte(tiupTestRules))
	require.NoError(t, err)
	return cfg
}

func tiupArtifactConfig(version string, files ...string) map[string]any {
	packages := make([]any, 0, len(files))
	for _, file := range files {
		packages = append(packages, map[string]any{
			"file":        file,
			"description": "package " + file,
			"entrypoint":  "bin/server",
			"standalone":  true, // must be ignored on purpose
		})
	}
	return map[string]any{
		"org.opencontainers.image.version": version,
		"net.pingcap.tibuild.os":           "linux",
		"net.pingcap.tibuild.architecture": "amd64",
		"net.pingcap.tibuild.tiup":         packages,
	}
}

func TestMatchTiUPRuleBranches(t *testing.T) {
	cfg := tiupTestConfig(t)

	cases := []struct {
		name    string
		url     string
		targets []delivery.TiUPTarget
	}{
		{
			name: "nightly goes to both mirrors",
			url:  "example.com/hub/pingcap/tidb/package:master_linux_amd64",
			targets: []delivery.TiUPTarget{
				{Target: delivery.TiUPTargetStaging, Mirror: "staging", Nightly: true},
				{Target: delivery.TiUPTargetProd, Mirror: "prod", Nightly: true},
			},
		},
		{
			name: "release branch goes to staging only",
			url:  "example.com/hub/pingcap/tidb/package:release-8.5_linux_amd64",
			targets: []delivery.TiUPTarget{
				{Target: delivery.TiUPTargetStaging, Mirror: "staging"},
			},
		},
		{
			name: "ga extracts the version from the tag",
			url:  "example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
			targets: []delivery.TiUPTarget{
				{Target: delivery.TiUPTargetStaging, Mirror: "staging", Version: "v8.5.6"},
				{Target: delivery.TiUPTargetProd, Mirror: "prod", Version: "v8.5.6"},
			},
		},
		{
			name: "rc tag keeps its suffix in the capture group",
			url:  "example.com/hub/pingcap/tidb/package:v8.1.0-rc.1_linux_amd64",
			targets: []delivery.TiUPTarget{
				{Target: delivery.TiUPTargetStaging, Mirror: "staging", Version: "v8.1.0-rc.1"},
				{Target: delivery.TiUPTargetProd, Mirror: "prod", Version: "v8.1.0-rc.1"},
			},
		},
		{
			name: "rust node publishes a fixed version",
			url:  "example.com/hub/pingcap/tidb/package:hparser-integration-rust_linux_amd64",
			targets: []delivery.TiUPTarget{
				{Target: delivery.TiUPTargetStaging, Mirror: "staging", Version: "v9.0.0-beta.2.pre.rust"},
			},
		},
		{
			name: "nextgen uses its own repository pattern",
			url:  "example.com/tidbx/pingcap/tidb/package:v26.3.1-nextgen.202603.1_linux_amd64",
			targets: []delivery.TiUPTarget{
				{Target: delivery.TiUPTargetStaging, Mirror: "staging", Version: "v26.3.1-nextgen.202603.1"},
			},
		},
		{
			name:    "windows tags match nothing",
			url:     "example.com/hub/pingcap/tidb/package:v8.5.6_windows_amd64",
			targets: nil,
		},
		{
			name:    "unknown repository matches nothing",
			url:     "example.com/other/pingcap/tidb/package:master_linux_amd64",
			targets: nil,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			targets, err := cfg.MatchTiUP(tc.url)
			require.NoError(t, err)
			require.Equal(t, tc.targets, targets)
		})
	}
}

func TestPlanTiUPBuildsPackagesPerTarget(t *testing.T) {
	cfg := tiupTestConfig(t)
	config := tiupArtifactConfig("v8.5.6",
		"tidb-v8.5.6-linux-amd64.tar.gz",
		"br-v8.5.6-linux-amd64.tar.gz",
	)

	plans, err := cfg.PlanTiUP("example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64", config)
	require.NoError(t, err)
	require.Len(t, plans, 2)

	for _, plan := range plans {
		require.Equal(t, "v8.5.6", plan.Version)
		require.Equal(t, "linux", plan.Packages[0].OS)
		require.Equal(t, "amd64", plan.Packages[0].Arch)
		require.Equal(t, "tidb", plan.Packages[0].Name)
		require.Equal(t, "bin/server", plan.Packages[0].EntryPoint)
		require.Equal(t, "br", plan.Packages[1].Name)
		require.False(t, plan.Packages[1].Standalone, "the legacy path never forwarded standalone")
		require.Equal(t, "br-v8.5.6-linux-amd64.tar.gz", plan.Packages[1].File)
	}
	require.Equal(t, delivery.TiUPTargetProd, plans[1].Target)
	require.Equal(t, "prod", plans[1].Mirror)
}

func TestPlanTiUPNightlyVersionUsesTheArtifactVersion(t *testing.T) {
	cfg := tiupTestConfig(t)
	config := tiupArtifactConfig("v9.0.0-beta.2.pre-943-g9aa228717e", "tidb-v9.0.0-beta.2.pre-943-g9aa228717e-linux-amd64.tar.gz")

	plans, err := cfg.PlanTiUP("example.com/hub/pingcap/tidb/package:master_linux_amd64", config)
	require.NoError(t, err)
	require.Len(t, plans, 2)
	for _, plan := range plans {
		require.Equal(t, "v9.0.0-beta.2.pre-nightly", plan.Version)
		require.True(t, plan.Nightly)
		require.Equal(t, "v9.0.0-beta.2.pre-nightly", plan.Packages[0].Version)
		require.True(t, delivery.IsNightlyTiUP(plan.Version))
	}
}

func TestPlanTiUPRejectsUnsupportedMirror(t *testing.T) {
	cfg, err := delivery.Parse([]byte(`
tiup_publish_rules:
  ^example.com/hub/.+/package$:
    - description: broken
      tags_regex:
        - ^master_linux_amd64$
      dest_mirrors: [sandbox]
`))
	require.NoError(t, err)

	_, err = cfg.MatchTiUP("example.com/hub/pingcap/tidb/package:master_linux_amd64")
	require.ErrorContains(t, err, `unknown tiup mirror "sandbox"`)
}

func TestPlanTiUPRejectsMalformedArtifact(t *testing.T) {
	cfg := tiupTestConfig(t)

	// A digest-only reference cannot be planned: the rules match on tags.
	_, err := cfg.PlanTiUP("example.com/hub/pingcap/tidb/package@sha256:"+strings.Repeat("a", 64), nil)
	require.ErrorIs(t, err, delivery.ErrMissingTag)

	// No tag at all.
	_, err = cfg.MatchTiUP("example.com/hub/pingcap/tidb/package")
	require.ErrorIs(t, err, delivery.ErrMissingTag)

	// Config without a package list.
	_, err = cfg.PlanTiUP("example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64", map[string]any{
		"net.pingcap.tibuild.os":           "linux",
		"net.pingcap.tibuild.architecture": "amd64",
	})
	require.ErrorIs(t, err, delivery.ErrNoTiUPArtifact)

	// Config with a package that has no file name.
	_, err = cfg.PlanTiUP("example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64", map[string]any{
		"org.opencontainers.image.version": "v8.5.6",
		"net.pingcap.tibuild.os":           "linux",
		"net.pingcap.tibuild.architecture": "amd64",
		"net.pingcap.tibuild.tiup":         []any{map[string]any{"entrypoint": "x"}},
	})
	require.ErrorContains(t, err, "no file")

	// Config missing the platform labels.
	_, err = cfg.PlanTiUP("example.com/hub/pingcap/tidb/package:v8.5.6_linux_amd64", map[string]any{
		"net.pingcap.tibuild.tiup": []any{map[string]any{"file": "tidb-v8.5.6-linux-amd64.tar.gz"}},
	})
	require.ErrorContains(t, err, "net.pingcap.tibuild.os")
}
