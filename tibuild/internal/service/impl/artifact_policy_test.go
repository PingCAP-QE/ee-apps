package impl

import (
	"net/http"
	"testing"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/artifact"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
)

// An unset or broken image-sync policy must deny every request. Treating an
// empty regex as "validate nothing" turned a missing config key into an open
// relay for arbitrary image copies.
func TestImageSyncPolicyFailsClosed(t *testing.T) {
	const sourceRe = `^hub\.pingcap\.net/(pingcap|tikv)/[\w-]+:v\d+\.\d+\.\d+-\d{8,}.*$`
	const targetRe = `^pingcap/[\w-]+:v\d+\.\d+\.\d+-\d{8,}.*$`

	tests := []struct {
		name     string
		cfg      config.ImageSync
		req      artifact.ImageSyncRequest
		wantCode int    // 0 means "no error"
		wantMsg  string
	}{
		{
			name:     "both regexes unset denies",
			cfg:      config.ImageSync{},
			req:      artifact.ImageSyncRequest{Source: "hub.pingcap.net/pingcap/tidb:v8.5.4-20260101", Target: "pingcap/tidb:v8.5.4-20260101"},
			wantCode: http.StatusForbidden,
			wantMsg:  "not configured",
		},
		{
			name:     "only the source regex set denies",
			cfg:      config.ImageSync{SourceRegx: sourceRe},
			req:      artifact.ImageSyncRequest{Source: "hub.pingcap.net/pingcap/tidb:v8.5.4-20260101", Target: "pingcap/tidb:v8.5.4-20260101"},
			wantCode: http.StatusForbidden,
			wantMsg:  "target_regx is not configured",
		},
		{
			name:     "an invalid source regex with no previous value denies",
			cfg:      config.ImageSync{SourceRegx: "([", TargetRegx: targetRe},
			req:      artifact.ImageSyncRequest{Source: "hub.pingcap.net/pingcap/tidb:v8.5.4-20260101", Target: "pingcap/tidb:v8.5.4-20260101"},
			wantCode: http.StatusForbidden,
			wantMsg:  "source_regx is invalid",
		},
		{
			name:     "a configured policy still rejects a source outside it",
			cfg:      config.ImageSync{SourceRegx: sourceRe, TargetRegx: targetRe},
			req:      artifact.ImageSyncRequest{Source: "evil.example.com/pingcap/tidb:v8.5.4-20260101", Target: "pingcap/tidb:v8.5.4-20260101"},
			wantCode: http.StatusBadRequest,
			wantMsg:  "source image not valid",
		},
		{
			name: "a configured policy accepts a matching pair",
			cfg:  config.ImageSync{SourceRegx: sourceRe, TargetRegx: targetRe},
			req:  artifact.ImageSyncRequest{Source: "hub.pingcap.net/pingcap/tidb:v8.5.4-20260101", Target: "pingcap/tidb:v8.5.4-20260101"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			logger := zerolog.Nop()
			svc := &artifactsrvc{logger: &logger}
			svc.applyConfig(&config.Service{ImageSync: tc.cfg})

			err := svc.validate(&tc.req)
			if tc.wantCode == 0 {
				require.NoError(t, err)
				return
			}
			require.Error(t, err)
			var httpErr *artifact.HTTPError
			require.ErrorAs(t, err, &httpErr)
			require.Equal(t, tc.wantCode, httpErr.Code)
			require.Contains(t, httpErr.Message, tc.wantMsg)
		})
	}
}

// A reload that drops the regexes must disable the endpoint, not re-open it.
func TestImageSyncPolicyReloadCanDisable(t *testing.T) {
	logger := zerolog.Nop()
	svc := &artifactsrvc{logger: &logger}
	svc.applyConfig(&config.Service{ImageSync: config.ImageSync{
		SourceRegx: `^hub\.pingcap\.net/.*$`,
		TargetRegx: `^pingcap/.*$`,
	}})
	req := &artifact.ImageSyncRequest{Source: "hub.pingcap.net/pingcap/tidb:v1", Target: "pingcap/tidb:v1"}
	require.NoError(t, svc.validate(req))

	svc.applyConfig(&config.Service{ImageSync: config.ImageSync{}})
	err := svc.validate(req)
	require.Error(t, err)
	var httpErr *artifact.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusForbidden, httpErr.Code)
}
