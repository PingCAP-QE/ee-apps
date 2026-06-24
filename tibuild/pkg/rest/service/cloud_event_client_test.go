package service

import (
	"context"
	"os"
	"testing"

	"github.com/stretchr/testify/require"
)

func sampleDevBuild() DevBuild {
	obj := DevBuild{
		ID:   1,
		Meta: DevBuildMeta{CreatedBy: "some@pingcap.com"},
		Spec: DevBuildSpec{
			Product: ProductPd, GitRef: "branch/master", Version: "v7.5.0",
			Edition: EditionCommunity, GitHash: "e264a6143f6d8badde7783577c5fced5dcb2c39f",
		}}
	fillWithDefaults(&obj)
	if err := validateReq(obj); err != nil {
		panic(err)
	}
	return obj
}

func TestNewEvent(t *testing.T) {
	dev := sampleDevBuild()
	dev.Spec.GitHash = "754095a9f460dcf31f053045cfedfb00b9ad8e81"

	evs, err := newDevBuildCloudEvents(dev)
	require.NoError(t, err)
	require.Len(t, evs, 2)
	require.Equal(t, "linux/amd64", evs[0].Extensions()["paramplatform"])
	require.Equal(t, "linux/arm64", evs[1].Extensions()["paramplatform"])
}

func TestNewEventNormalizesNextGenProfile(t *testing.T) {
	dev := sampleDevBuild()
	dev.Spec.Edition = EditionNextGenOld
	dev.Spec.GitHash = "754095a9f460dcf31f053045cfedfb00b9ad8e81"

	evs, err := newDevBuildCloudEvents(dev)
	require.NoError(t, err)
	require.Len(t, evs, 2)
	require.Equal(t, "nextgen", evs[0].Extensions()["paramprofile"])
	require.Equal(t, "nextgen", evs[1].Extensions()["paramprofile"])
}

func TestTrigger(t *testing.T) {
	if os.Getenv("TEST_FANOUT") == "" {
		t.Skip("Skipping send event")
	}
	trigger := NewCEClient("http://localhost:8000")
	err := trigger.TriggerDevBuild(context.TODO(), sampleDevBuild())
	require.NoError(t, err)
}
