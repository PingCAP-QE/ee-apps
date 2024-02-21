package service

import (
	"context"
	"encoding/json"
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
			Edition: CommunityEdition, GitHash: "e264a6143f6d8badde7783577c5fced5dcb2c39f",
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

	ev, err := NewDevBuildCloudEvent(dev)
	require.NoError(t, err)
	js, err := json.Marshal(ev)
	require.NoError(t, err)
	expected := `{"specversion":"1.0","id":"","source":"tibuild.pingcap.net/api/devbuilds/1","type":"net.pingcap.tibuild.devbuild.push","subject":"1","datacontenttype":"application/json","data":{"ref":"refs/heads/master","after":"754095a9f460dcf31f053045cfedfb00b9ad8e81", "before":"00000000000000000000000000000000000000000","repository":{"name":"pd","owner":{"login":"tikv"},"clone_url":"https://github.com/tikv/pd"}},"user":"some@pingcap.com"}`
	require.JSONEq(t, expected, string(js))
}

func TestTrigger(t *testing.T) {
	if os.Getenv("TEST_FANOUT") == "" {
		t.Skip("Skipping send event")
	}
	trigger := NewCEClient("http://localhost:8000")
	err := trigger.TriggerDevBuild(context.TODO(), sampleDevBuild())
	require.NoError(t, err)
}
