package handler

import (
	"slices"
	"testing"
)

func TestParseCommandDevbuildTrigger(t *testing.T) {
	tests := []struct {
		name     string
		args     []string
		expected *triggerParams
		wantErr  bool
	}{
		{
			name: "basic usage with required flags",
			args: []string{"-product", "tidb", "-version", "v6.1.0", "-gitRef", "master"},
			expected: &triggerParams{
				product:    "tidb",
				version:    "v6.1.0",
				gitRef:     "master",
				edition:    "community", // default
				platform:   "",
				buildEnvs:  []string{},
				pushGCR:    false,
				hotfix:     false,
				dryRun:     false,
				githubRepo: "",
				features:   "",
			},
			wantErr: false,
		},
		{
			name: "missing product flag",
			args: []string{"-version", "v6.1.0", "-gitRef", "master"}, // missing product
			expected: nil,
			wantErr:  true,
		},
		{
			name: "missing version flag",
			args: []string{"-product", "tidb", "-gitRef", "master"}, // missing version
			expected: nil,
			wantErr:  true,
		},
		{
			name: "missing gitRef flag",
			args: []string{"-product", "tidb", "-version", "v6.1.0"}, // missing gitRef
			expected: nil,
			wantErr:  true,
		},
		{
			name:     "invalid flag",
			args:     []string{"-invalidFlag", "value", "-product", "tidb", "-version", "v6.1.0", "-gitRef", "master"},
			expected: nil,
			wantErr:  true,
		},
		{
			name: "with optional flags",
			args: []string{
				"-product", "tidb",
				"-version", "v6.1.0",
				"-gitRef", "master",
				"-edition", "enterprise",
				"-platform", "linux/amd64",
				"-pushGCR",
				"-hotfix",
				"-dryrun",
				"-githubRepo", "pingcap/tidb",
				"-features", "failpoint",
				"-buildEnv", "GO_VERSION=1.17",
				"-buildEnv", "GOPROXY=https://goproxy.io",
			},
			expected: &triggerParams{
				product:    "tidb",
				version:    "v6.1.0",
				gitRef:     "master",
				edition:    "enterprise",
				platform:   "linux/amd64",
				buildEnvs:  []string{"GO_VERSION=1.17", "GOPROXY=https://goproxy.io"},
				pushGCR:    true,
				hotfix:     true,
				dryRun:     true,
				githubRepo: "pingcap/tidb",
				features:   "failpoint",
			},
			wantErr: false,
		},
		{
			name: "with long form flags",
			args: []string{
				"--product", "tidb",
				"--version", "v6.1.0",
				"--gitRef", "master",
				"--edition", "enterprise",
				"--platform", "linux/amd64",
			},
			expected: &triggerParams{
				product:    "tidb",
				version:    "v6.1.0",
				gitRef:     "master",
				edition:    "enterprise",
				platform:   "linux/amd64",
				buildEnvs:  []string{},
				pushGCR:    false,
				hotfix:     false,
				dryRun:     false,
				githubRepo: "",
				features:   "",
			},
			wantErr: false,
		},
		{
			name: "with plugin reference",
			args: []string{
				"--product", "tidb",
				"--version", "v6.1.0",
				"--gitRef", "master",
				"--pluginGitRef", "main",
			},
			expected: &triggerParams{
				product:      "tidb",
				version:      "v6.1.0",
				gitRef:       "master",
				edition:      "community",
				platform:     "",
				pluginGitRef: "main",
				buildEnvs:    []string{},
				pushGCR:      false,
				hotfix:       false,
				dryRun:       false,
				githubRepo:   "",
				features:     "",
			},
			wantErr: false,
		},
		{
			name: "with docker build parameters",
			args: []string{
				"--product", "tidb",
				"--version", "v6.1.0",
				"--gitRef", "master",
				"--productDockerfile", "dockerfile/tidb.Dockerfile",
				"--productBaseImg", "alpine:3.15",
				"--builderImg", "golang:1.17-alpine",
				"--targetImg", "pingcap/tidb:latest",
				"--engine", "jenkins",
			},
			expected: &triggerParams{
				product:           "tidb",
				version:           "v6.1.0",
				gitRef:            "master",
				edition:           "community",
				platform:          "",
				productDockerfile: "dockerfile/tidb.Dockerfile",
				productBaseImg:    "alpine:3.15",
				builderImg:        "golang:1.17-alpine",
				targetImg:         "pingcap/tidb:latest",
				engine:            "jenkins",
				buildEnvs:         []string{},
				pushGCR:           false,
				hotfix:            false,
				dryRun:            false,
				githubRepo:        "",
				features:          "",
			},
			wantErr: false,
		},
		{
			name: "short form flags for edition and platform",
			args: []string{
				"--product", "tidb",
				"--version", "v6.1.0",
				"--gitRef", "master",
				"-e", "enterprise",
				"-p", "linux/amd64",
			},
			expected: &triggerParams{
				product:    "tidb",
				version:    "v6.1.0",
				gitRef:     "master",
				edition:    "enterprise",
				platform:   "linux/amd64",
				buildEnvs:  []string{},
				pushGCR:    false,
				hotfix:     false,
				dryRun:     false,
				githubRepo: "",
				features:   "",
			},
			wantErr: false,
		},
		{
			name: "mixed option order",
			args: []string{
				"-gitRef", "master",
				"-product", "tidb",
				"-version", "v6.1.0",
			},
			expected: &triggerParams{
				product:    "tidb",
				version:    "v6.1.0",
				gitRef:     "master",
				edition:    "community",
				platform:   "",
				buildEnvs:  []string{},
				pushGCR:    false,
				hotfix:     false,
				dryRun:     false,
				githubRepo: "",
				features:   "",
			},
			wantErr: false,
		},
		{
			name: "tekton experiment profile",
			args: []string{
				"-product", "tidb",
				"-version", "v6.1.0",
				"-gitRef", "master",
				"-e", "experiment",
				"-p", "linux/amd64",
				"--engine", "tekton",
			},
			expected: &triggerParams{
				product:    "tidb",
				version:    "v6.1.0",
				gitRef:     "master",
				edition:    "experiment",
				platform:   "linux/amd64",
				buildEnvs:  []string{},
				pushGCR:    false,
				hotfix:     false,
				dryRun:     false,
				githubRepo: "",
				features:   "",
				engine:     "tekton",
			},
			wantErr: false,
		},
		{
			name:     "no arguments",
			args:     []string{},
			expected: nil,
			wantErr:  true,
		},
	}

	compareTriggerParams := func(got, expected *triggerParams) bool {
		// t.Helper()
		if got == nil && expected == nil {
			return true
		}
		if got == nil || expected == nil {
			return false
		}

		if got.product != expected.product {
			t.Logf("product mismatch: got %s, want %s", got.product, expected.product)
			return false
		}
		if got.edition != expected.edition {
			t.Logf("edition mismatch: got %s, want %s", got.edition, expected.edition)
			return false
		}
		if got.version != expected.version {
			t.Logf("version mismatch: got %s, want %s", got.version, expected.version)
			return false
		}
		if got.platform != expected.platform {
			t.Logf("platform mismatch: got %s, want %s", got.platform, expected.platform)
			return false
		}
		if got.gitRef != expected.gitRef {
			t.Logf("gitRef mismatch: got %s, want %s", got.gitRef, expected.gitRef)
			return false
		}
		if got.pluginGitRef != expected.pluginGitRef {
			t.Logf("pluginGitRef mismatch: got %s, want %s", got.pluginGitRef, expected.pluginGitRef)
			return false
		}
		if got.githubRepo != expected.githubRepo {
			t.Logf("githubRepo mismatch: got %s, want %s", got.githubRepo, expected.githubRepo)
			return false
		}
		if got.features != expected.features {
			t.Logf("features mismatch: got %s, want %s", got.features, expected.features)
			return false
		}
		if got.productDockerfile != expected.productDockerfile {
			t.Logf("productDockerfile mismatch: got %s, want %s", got.productDockerfile, expected.productDockerfile)
			return false
		}
		if got.productBaseImg != expected.productBaseImg {
			t.Logf("productBaseImg mismatch: got %s, want %s", got.productBaseImg, expected.productBaseImg)
			return false
		}
		if got.builderImg != expected.builderImg {
			t.Logf("builderImg mismatch: got %s, want %s", got.builderImg, expected.builderImg)
			return false
		}
		if got.targetImg != expected.targetImg {
			t.Logf("targetImg mismatch: got %s, want %s", got.targetImg, expected.targetImg)
			return false
		}
		if got.engine != expected.engine {
			t.Logf("engine mismatch: got %s, want %s", got.engine, expected.engine)
			return false
		}
		if !slices.Equal(got.buildEnvs, expected.buildEnvs) {
			t.Logf("buildEnvs mismatch: got %v, want %v", got.buildEnvs, expected.buildEnvs)
			return false
		}
		if got.hotfix != expected.hotfix {
			t.Logf("hotfix mismatch: got %v, want %v", got.hotfix, expected.hotfix)
			return false
		}
		if got.pushGCR != expected.pushGCR {
			t.Logf("pushGCR mismatch: got %v, want %v", got.pushGCR, expected.pushGCR)
			return false
		}
		if got.dryRun != expected.dryRun {
			t.Logf("dryRun mismatch: got %v, want %v", got.dryRun, expected.dryRun)
			return false
		}

		return true
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := parseCommandDevbuildTrigger(tt.args)
			if (err != nil) != tt.wantErr {
				t.Errorf("parseCommandDevbuildTrigger() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			if !compareTriggerParams(got, tt.expected) {
				t.Errorf("parseCommandDevbuildTrigger() = %v, want %v", got, tt.expected)
			}
		})
	}
}

// TestArrayFlags tests the custom flag implementation for array flags
func TestArrayFlags(t *testing.T) {
	var flags arrayFlags

	// Test initial state
	if flags.String() != "" {
		t.Errorf("Expected empty string for initial arrayFlags.String(), got %q", flags.String())
	}

	// Test adding values
	flags.Set("value1")
	if flags.String() != "value1" {
		t.Errorf("Expected 'value1' after first Set(), got %q", flags.String())
	}

	flags.Set("value2")
	if flags.String() != "value1,value2" {
		t.Errorf("Expected 'value1,value2' after second Set(), got %q", flags.String())
	}

	// Test that the underlying slice has the expected values
	expected := arrayFlags{"value1", "value2"}
	if !slices.Equal(flags, expected) {
		t.Errorf("Expected %v, got %v", expected, flags)
	}
}