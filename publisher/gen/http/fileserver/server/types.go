// Code generated by goa v3.19.1, DO NOT EDIT.
//
// fileserver HTTP server types
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/publisher/design

package server

import (
	fileserver "github.com/PingCAP-QE/ee-apps/publisher/gen/fileserver"
	goa "goa.design/goa/v3/pkg"
)

// RequestToPublishRequestBody is the type of the "fileserver" service
// "request-to-publish" endpoint HTTP request body.
type RequestToPublishRequestBody struct {
	// The full url of the pushed OCI artifact, contain the tag part. It will parse
	// the repo from it.
	ArtifactURL *string `form:"artifact_url,omitempty" json:"artifact_url,omitempty" xml:"artifact_url,omitempty"`
}

// NewRequestToPublishPayload builds a fileserver service request-to-publish
// endpoint payload.
func NewRequestToPublishPayload(body *RequestToPublishRequestBody) *fileserver.RequestToPublishPayload {
	v := &fileserver.RequestToPublishPayload{
		ArtifactURL: *body.ArtifactURL,
	}

	return v
}

// NewQueryPublishingStatusPayload builds a fileserver service
// query-publishing-status endpoint payload.
func NewQueryPublishingStatusPayload(requestID string) *fileserver.QueryPublishingStatusPayload {
	v := &fileserver.QueryPublishingStatusPayload{}
	v.RequestID = requestID

	return v
}

// ValidateRequestToPublishRequestBody runs the validations defined on
// Request-To-PublishRequestBody
func ValidateRequestToPublishRequestBody(body *RequestToPublishRequestBody) (err error) {
	if body.ArtifactURL == nil {
		err = goa.MergeErrors(err, goa.MissingFieldError("artifact_url", "body"))
	}
	return
}