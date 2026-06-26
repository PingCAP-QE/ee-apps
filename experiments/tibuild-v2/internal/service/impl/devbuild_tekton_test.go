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
