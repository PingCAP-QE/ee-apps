package service

import (
	_ "embed"
	"encoding/json"
	"testing"

	rest "github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/stretchr/testify/require"
)

//go:embed tekton_event.json
var tekton_event_json []byte

func TestEventToDevbuildTekton(t *testing.T) {
	ev := cloudevents.Event{}
	err := json.Unmarshal(tekton_event_json, &ev)
	require.NoError(t, err)
	pipeline, bid, err := eventToDevbuildTekton(ev)
	require.NoError(t, err)
	require.NotZero(t, bid)
	require.Equal(t, 1, len(pipeline.OrasArtifacts))
	require.Equal(t, 1, len(pipeline.Images))
	require.Equal(t, rest.LinuxArm64, pipeline.Platform)
}
