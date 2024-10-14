// Code generated by goa v3.19.1, DO NOT EDIT.
//
// tiup HTTP client CLI support package
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/publisher/design

package client

import (
	"encoding/json"
	"fmt"

	tiup "github.com/PingCAP-QE/ee-apps/publisher/gen/tiup"
)

// BuildRequestToPublishPayload builds the payload for the tiup
// request-to-publish endpoint from CLI flags.
func BuildRequestToPublishPayload(tiupRequestToPublishBody string) (*tiup.RequestToPublishPayload, error) {
	var err error
	var body RequestToPublishRequestBody
	{
		err = json.Unmarshal([]byte(tiupRequestToPublishBody), &body)
		if err != nil {
			return nil, fmt.Errorf("invalid JSON for body, \nerror: %s, \nexample of valid JSON:\n%s", err, "'{\n      \"artifact_url\": \"A facilis.\",\n      \"request_id\": \"Sequi placeat blanditiis est iusto quia eum.\",\n      \"tiup-mirror\": \"Sunt voluptates.\",\n      \"version\": \"Rerum consectetur deleniti.\"\n   }'")
		}
	}
	v := &tiup.RequestToPublishPayload{
		ArtifactURL: body.ArtifactURL,
		Version:     body.Version,
		TiupMirror:  body.TiupMirror,
		RequestID:   body.RequestID,
	}

	return v, nil
}

// BuildQueryPublishingStatusPayload builds the payload for the tiup
// query-publishing-status endpoint from CLI flags.
func BuildQueryPublishingStatusPayload(tiupQueryPublishingStatusRequestID string) (*tiup.QueryPublishingStatusPayload, error) {
	var requestID string
	{
		requestID = tiupQueryPublishingStatusRequestID
	}
	v := &tiup.QueryPublishingStatusPayload{}
	v.RequestID = requestID

	return v, nil
}