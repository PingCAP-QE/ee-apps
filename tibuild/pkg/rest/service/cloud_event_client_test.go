package service

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
)

func sampleDevBuild() DevBuild {
	obj := DevBuild{
		ID:   1,
		Meta: DevBuildMeta{CreatedBy: "some@pingcap.com"},
		Spec: DevBuildSpec{Product: ProductBr, GitRef: "branch/master", Version: "v7.5.0"}}
	fillWithDefaults(&obj)
	return obj
}

func TestNewEvent(t *testing.T) {
	ev, err := NewDevBuildCloudEvent(sampleDevBuild())
	require.NoError(t, err)
	_, err = json.Marshal(ev)
	require.NoError(t, err)
}

func TestTrigger(t *testing.T) {
	trigger := NewCEClient("http://localhost:8000")
	err := trigger.TriggerDevBuild(context.TODO(), sampleDevBuild())
	require.Error(t, err)
}
