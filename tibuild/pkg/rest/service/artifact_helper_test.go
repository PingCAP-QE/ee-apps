package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestPublishImage(t *testing.T) {
	mockedJenkins := &mockJenkins{}
	utils := ArtifactHelper{jenkins: mockedJenkins}
	_, err := utils.SyncImage(context.TODO(), ImageSyncRequest{Source: "hub.pingcap.net/repo/image:tag", Target: "pingcap/image:tag2"})
	assert.ErrorIsf(t, err, ErrBadRequest, "source image")
	_, err = utils.SyncImage(context.TODO(), ImageSyncRequest{Source: "hub.pingcap.net/pingcap/image:v6.1.1-20230101-123", Target: "pingcap/image:v6.1.1-20230101"})
	assert.NoError(t, err)
	assert.Equal(t, map[string]string{"SOURCE_IMAGE": "hub.pingcap.net/pingcap/image:v6.1.1-20230101-123", "TARGET_IMAGE": "pingcap/image:v6.1.1-20230101"}, mockedJenkins.params)
}
