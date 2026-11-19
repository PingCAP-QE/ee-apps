package impl

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
)

func TestRepositoryAllowed(t *testing.T) {
	tests := []struct {
		name       string
		patterns   []string
		repository string
		expected   bool
	}{
		{"exact match", []string{"hub.pingcap.net/pingcap/tidb"}, "hub.pingcap.net/pingcap/tidb", true},
		{"exact match is case insensitive", []string{"HUB.pingcap.net/pingcap/tidb"}, "hub.pingcap.net/pingcap/tidb", true},
		{"prefix glob", []string{"hub.pingcap.net/pingcap/*"}, "hub.pingcap.net/pingcap/tikv", true},
		{"prefix glob matches the bare prefix", []string{"hub.pingcap.net/pingcap/*"}, "hub.pingcap.net/pingcap", true},
		{"prefix glob does not cross the separator", []string{"hub.pingcap.net/pingcap/*"}, "hub.pingcap.net/pingcapx/tidb", false},
		{"other host is refused", []string{"hub.pingcap.net/pingcap/*"}, "evil.example.com/pingcap/tidb", false},
		{"empty list allows nothing", nil, "hub.pingcap.net/pingcap/tidb", false},
		{"blank pattern is ignored", []string{"  "}, "hub.pingcap.net/pingcap/tidb", false},
		{"empty repository is refused", []string{"*"}, "", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			require.Equal(t, tc.expected, repositoryAllowed(tc.patterns, tc.repository))
		})
	}
}

// A sync that names a repository outside the allowlist must be refused before
// anything is sent to the ops platform, which would otherwise pull whatever it
// was handed.
func TestRequestSyncKernelImageRefusesRepositoryOutsideAllowlist(t *testing.T) {
	svc := newTidbcloudTestService(t, &OpsConfig{
		Stages: map[string]OpsStageConfig{
			"dev": {APIBaseURL: "http://127.0.0.1:1", APIKey: "dev-key"},
		},
	}, nil)
	svc.kernelImageAllowedRepositories = []string{"hub.pingcap.net/pingcap/*"}
	svc.kernelImageMetaReader = func(_ context.Context, _ string) kernelImageMeta {
		return kernelImageMeta{
			Repo:      "pingcap/tidb",
			GitTag:    "v8.5.4",
			CommitSHA: "abcdef0123456789abcdef0123456789abcdef01",
		}
	}

	_, err := svc.RequestSyncKernelImage(context.Background(), &tidbcloud.RequestSyncKernelImagePayload{
		Stage:  "dev",
		Images: []string{"evil.example.com/attacker/tidb:v8.5.4"},
	})
	require.ErrorContains(t, err, "is not allowed")
}

func TestRequestSyncKernelImageAcceptsAllowlistedRepository(t *testing.T) {
	svc := newTidbcloudTestService(t, &OpsConfig{
		Stages: map[string]OpsStageConfig{
			"dev": {APIBaseURL: "http://127.0.0.1:1", APIKey: "dev-key"},
		},
	}, nil)
	svc.kernelImageAllowedRepositories = []string{"hub.pingcap.net/pingcap/*"}
	svc.kernelImageMetaReader = func(_ context.Context, _ string) kernelImageMeta {
		return kernelImageMeta{
			Repo:      "pingcap/tidb",
			GitTag:    "v8.5.4",
			CommitSHA: "abcdef0123456789abcdef0123456789abcdef01",
		}
	}

	// The allowlist passes, so the call proceeds and fails on the unreachable
	// ops endpoint instead of the policy check.
	_, err := svc.RequestSyncKernelImage(context.Background(), &tidbcloud.RequestSyncKernelImagePayload{
		Stage:  "dev",
		Images: []string{"hub.pingcap.net/pingcap/tidb:v8.5.4"},
	})
	require.Error(t, err)
	require.NotContains(t, err.Error(), "is not allowed")
}
