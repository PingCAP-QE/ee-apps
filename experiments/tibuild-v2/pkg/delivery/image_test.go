package delivery_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// testConfig mirrors the interesting shapes of artifacts/packages/delivery.yaml
// (anchors, constant tags, tag_regex_replace, negative lookahead).
func testConfig() *delivery.Config {
	trunk := delivery.ImageRule{
		Description:      "delivery the trunk branch community images.",
		TagsRegex:        []string{`^(master|main)(-experiment)?$`},
		ConstantTags:     []string{"nightly"},
		DestRepositories: []string{"docker.io/pingcap/widget"},
	}
	ga := delivery.ImageRule{
		Description:      "delivery community images for GA images.",
		TagsRegex:        []string{`^v[0-9]+[.][0-9]+[.][0-9]+$`, `^v[0-9]+[.][0-9]+[.][0-9]+-(beta|rc|release)[.][0-9]+$`},
		DestRepositories: []string{"docker.io/pingcap/widget", "uhub.service.ucloud.cn/pingcap/widget"},
	}
	enterprise := delivery.ImageRule{
		Description:      "delivery the enterprise GA images.",
		TagsRegex:        []string{`^(v[0-9]+[.][0-9]+[.][0-9]+)-enterprise$`},
		TagRegexReplace:  "$1",
		DestRepositories: []string{"gcr.io/pingcap-public/dbaas/widget"},
	}
	// Copied from delivery.yaml: negative lookahead is not supported by RE2,
	// so the matcher has to use an ECMAScript-compatible engine.
	operator := delivery.ImageRule{
		Description: "delivery the version images, exclude pre release types.",
		TagsRegex: []string{
			`^(v[0-9]+[.][0-9]+[.][0-9]+)(-(?!pre([._]|$))(?![0-9a-f]{7,}(?:_|$)).*)?$`,
		},
		DestRepositories: []string{"docker.io/pingcap/operator"},
	}

	return &delivery.Config{
		ImageCopyRules: map[string][]delivery.ImageRule{
			"example.com/hub/pingcap/widget/image":   {trunk, ga, enterprise},
			"example.com/hub/pingcap/operator/image": {operator},
		},
	}
}

func TestMatchImageTrunkAddsConstantTags(t *testing.T) {
	plans, err := testConfig().MatchImage("example.com/hub/pingcap/widget/image:master")
	require.NoError(t, err)
	require.Equal(t, []delivery.TargetPlan{
		{Target: "docker.io", Dest: "docker.io/pingcap/widget", Tags: []string{"master", "nightly"}},
	}, plans)
}

func TestMatchImageGA(t *testing.T) {
	plans, err := testConfig().MatchImage("example.com/hub/pingcap/widget/image:v8.5.0")
	require.NoError(t, err)
	require.Equal(t, []delivery.TargetPlan{
		{Target: "docker.io", Dest: "docker.io/pingcap/widget", Tags: []string{"v8.5.0"}},
		{Target: "uhub.service.ucloud.cn", Dest: "uhub.service.ucloud.cn/pingcap/widget", Tags: []string{"v8.5.0"}},
	}, plans)
}

func TestMatchImageEnterpriseTagReplace(t *testing.T) {
	plans, err := testConfig().MatchImage("example.com/hub/pingcap/widget/image:v8.5.0-enterprise")
	require.NoError(t, err)
	require.Equal(t, []delivery.TargetPlan{
		{Target: "gcr.io", Dest: "gcr.io/pingcap-public/dbaas/widget", Tags: []string{"v8.5.0-enterprise", "v8.5.0"}},
	}, plans)
}

func TestMatchImageNegativeLookahead(t *testing.T) {
	cfg := testConfig()
	tests := map[string]bool{
		"v1.6.0":            true,
		"v1.6.0-release.1":  true,
		"v1.6.0-pre":        false,
		"v1.6.0-pre.1":      false,
		"v1.6.0-abcdef1234": false,
	}
	for tag, wantMatch := range tests {
		plans, err := cfg.MatchImage("example.com/hub/pingcap/operator/image:" + tag)
		require.NoError(t, err)
		require.Equal(t, wantMatch, len(plans) > 0, "tag %s", tag)
	}
}

func TestMatchImageUnknownRepositoryIsNotAnError(t *testing.T) {
	plans, err := testConfig().MatchImage("example.com/hub/pingcap/unknown/image:v8.5.0")
	require.NoError(t, err)
	require.Empty(t, plans)
}

func TestMatchImageDeprecatedIsSkipped(t *testing.T) {
	cfg := testConfig()
	cfg.Deprecated = []string{"example.com/hub/pingcap/widget/image"}

	plans, err := cfg.MatchImage("example.com/hub/pingcap/widget/image:v8.5.0")
	require.NoError(t, err)
	require.Empty(t, plans)
}

func TestMatchImageDigestIsNotResolved(t *testing.T) {
	plans, err := testConfig().MatchImage(
		"example.com/hub/pingcap/widget/image:v8.5.0@sha256:" + sha256Fixture)
	require.NoError(t, err)
	// The digest is carried through, but matching still uses the tag.
	require.Equal(t, "v8.5.0", plans[0].Tags[0])
}

func TestMatchImageRepeatedDestinationIsReportedOncePerRule(t *testing.T) {
	cfg := &delivery.Config{ImageCopyRules: map[string][]delivery.ImageRule{
		"example.com/hub/pingcap/widget/image": {
			{
				TagsRegex:        []string{`^v[0-9]+[.][0-9]+[.][0-9]+$`},
				DestRepositories: []string{"docker.io/pingcap/widget"},
			},
			{
				// Overlapping rule: the old generator emitted a second copy
				// command; the plan keeps both, deduplication happens when the
				// steps are created.
				TagsRegex:        []string{`^v[0-9]+[.][0-9]+[.][0-9]+$`},
				DestRepositories: []string{"docker.io/pingcap/widget"},
			},
		},
	}}

	plans, err := cfg.MatchImage("example.com/hub/pingcap/widget/image:v8.5.0")
	require.NoError(t, err)
	require.Len(t, plans, 2)
}

func TestMatchImageDeduplicatesTags(t *testing.T) {
	cfg := &delivery.Config{ImageCopyRules: map[string][]delivery.ImageRule{
		"example.com/hub/pingcap/widget/image": {
			{
				TagsRegex:        []string{`^v[0-9]+[.][0-9]+[.][0-9]+$`},
				ConstantTags:     []string{"v8.5.0", "latest"},
				DestRepositories: []string{"docker.io/pingcap/widget"},
			},
		},
	}}

	plans, err := cfg.MatchImage("example.com/hub/pingcap/widget/image:v8.5.0")
	require.NoError(t, err)
	require.Equal(t, []string{"v8.5.0", "latest"}, plans[0].Tags)
}

func TestTargetOf(t *testing.T) {
	tests := map[string]string{
		"docker.io/pingcap/widget":              "docker.io",
		"gcr.io/pingcap-public/dbaas/widget":    "gcr.io",
		"us-docker.pkg.dev/project/hub/widget":  "us-docker.pkg.dev",
		"localhost:5000/pingcap/widget":         "localhost:5000",
		"uhub.service.ucloud.cn/pingcap/widget": "uhub.service.ucloud.cn",
		"pingcap/widget":                        "docker.io",
	}
	for dest, want := range tests {
		require.Equal(t, want, delivery.TargetOf(dest), dest)
	}
}

func TestTargetsReturnsTheDestinationCatalogue(t *testing.T) {
	cfg := testConfig()
	require.Equal(t, []string{"docker.io", "gcr.io", "uhub.service.ucloud.cn"}, cfg.Targets())

	var nilCfg *delivery.Config
	require.Empty(t, nilCfg.Targets())
}
