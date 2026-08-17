package impl

import (
	"testing"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/stretchr/testify/require"
)

func TestNewDevBuildCloudEvent_PluginGitRef(t *testing.T) {
	s := &devbuildsrvc{}
	record := &ent.DevBuild{
		ID:          1,
		CreatedBy:   "test@pingcap.com",
		Product:     "tidb",
		Edition:     "community",
		GithubRepo:  "pingcap/tidb",
		GitRef:      "branch/master",
		PluginGitRef: "release-8.5.4",
	}

	event, err := s.newDevBuildCloudEvent(record, LinuxAmd64)
	require.NoError(t, err)
	require.Equal(t, "release-8.5.4", event.Extensions()["paramplugingitref"])
}

func TestNewDevBuildCloudEvent_PluginGitRefEmpty(t *testing.T) {
	s := &devbuildsrvc{}
	record := &ent.DevBuild{
		ID:          1,
		CreatedBy:   "test@pingcap.com",
		Product:     "tidb",
		Edition:     "community",
		GithubRepo:  "pingcap/tidb",
		GitRef:      "branch/master",
		PluginGitRef: "",
	}

	event, err := s.newDevBuildCloudEvent(record, LinuxAmd64)
	require.NoError(t, err)
	_, ok := event.Extensions()["paramplugingitref"]
	require.False(t, ok)
}

func TestOciArtifactToBinArtifacts_Platform(t *testing.T) {
	// binaries carry the platform of the pipeline run that produced them
	bins := ociArtifactToBinArtifacts("devbuild/1", "v8.5.5-20260127-69f3866",
		[]string{"pd-linux-amd64.tar.gz", "pd-linux-amd64.tar.gz.sha256"}, "linux/amd64")
	require.Len(t, bins, 1)
	require.NotNil(t, bins[0].Platform)
	require.Equal(t, "linux/amd64", *bins[0].Platform)
	require.NotNil(t, bins[0].Sha256OCIFile)

	// empty platform stays nil (no platform reported)
	bins = ociArtifactToBinArtifacts("devbuild/1", "v8.5.5-20260127-69f3866",
		[]string{"pd-linux-amd64.tar.gz"}, "")
	require.Len(t, bins, 1)
	require.Nil(t, bins[0].Platform)
}
