package impl

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/tidbcloud"
)

const tcmsTestSHA = "031069dfc0c70e839d996c9e1cf3d34930fc662f"

// tcmsRecorder captures the body posted to the TCMS image build endpoint.
type tcmsRecorder struct {
	requests int
	body     map[string]any
	raw      string
}

// newTCMSTestService returns a service wired to a fake TCMS endpoint and an
// image config reader that replays the given labels.
func newTCMSTestService(t *testing.T, labels map[string]string, readErr error) (*tidbcloudsrvc, *tcmsRecorder) {
	t.Helper()

	rec := &tcmsRecorder{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(r.Body)
		require.NoError(t, err)
		rec.requests++
		rec.raw = string(raw)
		require.NoError(t, json.Unmarshal(raw, &rec.body))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	t.Cleanup(server.Close)

	svc := newTidbcloudTestService(t, nil, &TestPlatformsConfig{
		TCMS: TCMSConfig{APIBaseURL: server.URL, AuthToken: "test-token"},
	})
	svc.readImageConfig = func(context.Context, string) ([]byte, error) {
		if readErr != nil {
			return nil, readErr
		}
		config := map[string]any{"config": map[string]any{"Labels": labels}}
		raw, err := json.Marshal(config)
		require.NoError(t, err)
		return raw, nil
	}
	return svc, rec
}

// stubImageWithoutLabels keeps a test off the registry: the endpoint always
// reads the image labels now, even when the caller supplied the git fields.
func stubImageWithoutLabels(t *testing.T, svc *tidbcloudsrvc) {
	t.Helper()
	svc.readImageConfig = func(context.Context, string) ([]byte, error) {
		return []byte(`{"config":{}}`), nil
	}
}

func imageLabels(extra map[string]string) map[string]string {
	labels := map[string]string{
		"org.opencontainers.image.source":   "https://github.com/pingcap/tidb",
		"org.opencontainers.image.revision": tcmsTestSHA,
		"org.opencontainers.image.ref.name": "refs/heads/release-8.5",
	}
	for k, v := range extra {
		labels[k] = v
	}
	return labels
}

func TestAddTidbxImageTagInTcmsCommitMetadata(t *testing.T) {
	svc, rec := newTCMSTestService(t, imageLabels(map[string]string{
		"net.pingcap.tibuild.git-commit-title": "fix: keep the  label   intact",
		"net.pingcap.tibuild.git-commit-time":  "2026-11-02T10:00:00+08:00",
	}), nil)

	res, err := svc.AddTidbxImageTagInTcms(context.Background(), &tidbcloud.AddTidbxImageTagInTcmsPayload{
		Image: "example.com/pingcap/tidb:v8.5.4-nextgen",
	})
	require.NoError(t, err)
	require.Equal(t, 1, rec.requests)

	require.Equal(t, "fix: keep the  label   intact", rec.body["commitTitle"])
	require.Equal(t, "2026-11-02T10:00:00+08:00", rec.body["commitTime"])
	require.Equal(t, "pingcap/tidb", rec.body["repo"])
	require.Equal(t, "release-8.5", rec.body["branch"])
	require.Equal(t, tcmsTestSHA, rec.body["sha"])
	require.Equal(t, "v8.5.4-nextgen", rec.body["imageTag"])

	// The response carries the same values back to the caller.
	require.NotNil(t, res.CommitTitle)
	require.Equal(t, "fix: keep the  label   intact", *res.CommitTitle)
	require.NotNil(t, res.CommitTime)
	require.Equal(t, "2026-11-02T10:00:00+08:00", *res.CommitTime)
}

func TestAddTidbxImageTagInTcmsCommitTitleTruncated(t *testing.T) {
	long := strings.Repeat("标题", 150) // 300 runes
	svc, rec := newTCMSTestService(t, imageLabels(map[string]string{
		"net.pingcap.tibuild.git-commit-title": long,
	}), nil)

	_, err := svc.AddTidbxImageTagInTcms(context.Background(), &tidbcloud.AddTidbxImageTagInTcmsPayload{
		Image: "example.com/pingcap/tidb:v8.5.4-nextgen",
	})
	require.NoError(t, err)

	title, ok := rec.body["commitTitle"].(string)
	require.True(t, ok)
	require.Equal(t, 255, utf8.RuneCountInString(title))
	require.True(t, strings.HasPrefix(long, title))
	require.NotContains(t, rec.body, "commitTime")
}

func TestAddTidbxImageTagInTcmsWithoutCommitLabels(t *testing.T) {
	svc, rec := newTCMSTestService(t, imageLabels(nil), nil)

	_, err := svc.AddTidbxImageTagInTcms(context.Background(), &tidbcloud.AddTidbxImageTagInTcmsPayload{
		Image: "example.com/pingcap/tidb:v8.5.4-nextgen",
	})
	require.NoError(t, err)

	require.NotContains(t, rec.raw, "commitTitle")
	require.NotContains(t, rec.raw, "commitTime")
	require.Equal(t, "pingcap/tidb", rec.body["repo"])
}

func TestAddTidbxImageTagInTcmsInvalidCommitTime(t *testing.T) {
	svc, rec := newTCMSTestService(t, imageLabels(map[string]string{
		"net.pingcap.tibuild.git-commit-title": "chore: bump deps",
		"net.pingcap.tibuild.git-commit-time":  "2026-11-02 10:00:00",
	}), nil)

	_, err := svc.AddTidbxImageTagInTcms(context.Background(), &tidbcloud.AddTidbxImageTagInTcmsPayload{
		Image: "example.com/pingcap/tidb:v8.5.4-nextgen",
	})
	require.NoError(t, err)

	require.Equal(t, "chore: bump deps", rec.body["commitTitle"])
	require.NotContains(t, rec.body, "commitTime")
}

func TestAddTidbxImageTagInTcmsExplicitGithubKeepsLabelCommit(t *testing.T) {
	svc, rec := newTCMSTestService(t, imageLabels(map[string]string{
		"net.pingcap.tibuild.git-commit-title": "feat: from labels",
		"net.pingcap.tibuild.git-commit-time":  "2026-11-02T10:00:00Z",
	}), nil)

	ref := "refs/heads/feature-x"
	_, err := svc.AddTidbxImageTagInTcms(context.Background(), &tidbcloud.AddTidbxImageTagInTcmsPayload{
		Image: "example.com/pingcap/tidb:v8.5.4-nextgen",
		Github: &struct {
			FullRepo  string
			Ref       *string
			CommitSha string
		}{FullRepo: "pingcap/tiflash", Ref: &ref, CommitSha: tcmsTestSHA},
	})
	require.NoError(t, err)

	// Git information stays with the explicit payload; commit metadata always
	// comes from the image labels.
	require.Equal(t, "pingcap/tiflash", rec.body["repo"])
	require.Equal(t, tcmsTestSHA, rec.body["sha"])
	require.Equal(t, "feature-x", rec.body["branch"])
	require.Equal(t, "feat: from labels", rec.body["commitTitle"])
	require.Equal(t, "2026-11-02T10:00:00Z", rec.body["commitTime"])
}

func TestAddTidbxImageTagInTcmsImageConfigError(t *testing.T) {
	svc, rec := newTCMSTestService(t, nil, errors.New("registry unavailable"))

	_, err := svc.AddTidbxImageTagInTcms(context.Background(), &tidbcloud.AddTidbxImageTagInTcmsPayload{
		Image: "example.com/pingcap/tidb:v8.5.4-nextgen",
	})
	require.NoError(t, err)

	// A registry failure only drops the metadata; the record is still written.
	require.Equal(t, 1, rec.requests)
	require.NotContains(t, rec.body, "repo")
	require.NotContains(t, rec.raw, "commitTitle")
	require.Equal(t, "v8.5.4-nextgen", rec.body["imageTag"])
}
