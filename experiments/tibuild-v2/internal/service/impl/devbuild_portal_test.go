package impl

import (
	"testing"

	"github.com/stretchr/testify/assert"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

func TestBuildArtifactURL(t *testing.T) {
	got := buildArtifactURL("https://downloads.example.com/oci", &devbuild.OciFile{
		Repo: "pingcap/tidb", Tag: "v8.5.0", File: "tidb-linux-amd64.tar.gz",
	})
	assert.Equal(t, "https://downloads.example.com/oci/pingcap/tidb?file=tidb-linux-amd64.tar.gz&tag=v8.5.0", got)
}

func TestValidGitRef(t *testing.T) {
	for _, ref := range []string{"branch/master", "tag/v8.5.0", "pull/123", "commit/0123456789abcdef0123456789abcdef01234567", "0123456789abcdef0123456789abcdef01234567"} {
		assert.True(t, validGitRef(ref), ref)
	}
	for _, ref := range []string{"master", "branch/", "pull/0", "pull/nope", "commit/abc"} {
		assert.False(t, validGitRef(ref), ref)
	}
}

func TestSafeErrorSummary(t *testing.T) {
	got := safeErrorSummary("pipeline failed token=super-secret\nfull logs follow")
	if assert.NotNil(t, got) {
		assert.Equal(t, "pipeline failed token=[redacted]", *got)
	}
	assert.Nil(t, safeErrorSummary(""))
}
