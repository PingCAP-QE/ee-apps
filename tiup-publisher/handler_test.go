package main

import (
	"testing"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/event"
	"github.com/stretchr/testify/assert"
)

const testMirrorURL = "http://tiup.pingcap.net:8988"

func TestHandler_Handle(t *testing.T) {
	t.Skip("manual test case")

	t.Run("Valid event data", func(tt *testing.T) {
		event := event.New()
		event.SetType(EventTypeTiupPublishRequest)
		event.SetSource("testSource")
		event.SetData("application/json", &PublishRequestEvent{
			From: From{
				Type: "oci",
				Oci: &FromOci{
					Repo: "hub.pingcap.net/pingcap/tidb/package",
					Tag:  "master_darwin_amd64",
					File: "tidb-v8.4.0-alpha-312-g8f0baf4444-darwin-amd64.tar.gz",
				},
			},
			Publish: PublishInfo{
				Name:        "tidb",
				Version:     "v8.4.0-alpha-n2",
				EntryPoint:  "tidb-server",
				OS:          "darwin",
				Arch:        "amd64",
				Description: "TiDB is an open source distributed HTAP database compatible with the MySQL protocol.",
				Standalone:  false,
			},
		})

		h := &Handler{testMirrorURL}
		result := h.Handle(event)
		assert.True(tt, cloudevents.IsACK(result))
		tt.Fail()
	})
}
