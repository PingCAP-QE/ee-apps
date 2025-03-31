// Code generated by goa v3.20.0, DO NOT EDIT.
//
// artifact HTTP client types
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/tibuild/internal/service/design -o
// ./service

package client

import (
	artifact "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/artifact"
	goa "goa.design/goa/v3/pkg"
)

// SyncImageRequestBody is the type of the "artifact" service "syncImage"
// endpoint HTTP request body.
type SyncImageRequestBody struct {
	Source string `form:"source" json:"source" xml:"source"`
	Target string `form:"target" json:"target" xml:"target"`
}

// SyncImageResponseBody is the type of the "artifact" service "syncImage"
// endpoint HTTP response body.
type SyncImageResponseBody struct {
	Source *string `form:"source,omitempty" json:"source,omitempty" xml:"source,omitempty"`
	Target *string `form:"target,omitempty" json:"target,omitempty" xml:"target,omitempty"`
}

// SyncImageBadRequestResponseBody is the type of the "artifact" service
// "syncImage" endpoint HTTP response body for the "BadRequest" error.
type SyncImageBadRequestResponseBody struct {
	Code    *int    `form:"code,omitempty" json:"code,omitempty" xml:"code,omitempty"`
	Message *string `form:"message,omitempty" json:"message,omitempty" xml:"message,omitempty"`
}

// SyncImageInternalServerErrorResponseBody is the type of the "artifact"
// service "syncImage" endpoint HTTP response body for the
// "InternalServerError" error.
type SyncImageInternalServerErrorResponseBody struct {
	Code    *int    `form:"code,omitempty" json:"code,omitempty" xml:"code,omitempty"`
	Message *string `form:"message,omitempty" json:"message,omitempty" xml:"message,omitempty"`
}

// NewSyncImageRequestBody builds the HTTP request body from the payload of the
// "syncImage" endpoint of the "artifact" service.
func NewSyncImageRequestBody(p *artifact.ImageSyncRequest) *SyncImageRequestBody {
	body := &SyncImageRequestBody{
		Source: p.Source,
		Target: p.Target,
	}
	return body
}

// NewSyncImageImageSyncRequestOK builds a "artifact" service "syncImage"
// endpoint result from a HTTP "OK" response.
func NewSyncImageImageSyncRequestOK(body *SyncImageResponseBody) *artifact.ImageSyncRequest {
	v := &artifact.ImageSyncRequest{
		Source: *body.Source,
		Target: *body.Target,
	}

	return v
}

// NewSyncImageBadRequest builds a artifact service syncImage endpoint
// BadRequest error.
func NewSyncImageBadRequest(body *SyncImageBadRequestResponseBody) *artifact.HTTPError {
	v := &artifact.HTTPError{
		Code:    *body.Code,
		Message: *body.Message,
	}

	return v
}

// NewSyncImageInternalServerError builds a artifact service syncImage endpoint
// InternalServerError error.
func NewSyncImageInternalServerError(body *SyncImageInternalServerErrorResponseBody) *artifact.HTTPError {
	v := &artifact.HTTPError{
		Code:    *body.Code,
		Message: *body.Message,
	}

	return v
}

// ValidateSyncImageResponseBody runs the validations defined on
// SyncImageResponseBody
func ValidateSyncImageResponseBody(body *SyncImageResponseBody) (err error) {
	if body.Source == nil {
		err = goa.MergeErrors(err, goa.MissingFieldError("source", "body"))
	}
	if body.Target == nil {
		err = goa.MergeErrors(err, goa.MissingFieldError("target", "body"))
	}
	return
}

// ValidateSyncImageBadRequestResponseBody runs the validations defined on
// syncImage_BadRequest_response_body
func ValidateSyncImageBadRequestResponseBody(body *SyncImageBadRequestResponseBody) (err error) {
	if body.Code == nil {
		err = goa.MergeErrors(err, goa.MissingFieldError("code", "body"))
	}
	if body.Message == nil {
		err = goa.MergeErrors(err, goa.MissingFieldError("message", "body"))
	}
	return
}

// ValidateSyncImageInternalServerErrorResponseBody runs the validations
// defined on syncImage_InternalServerError_response_body
func ValidateSyncImageInternalServerErrorResponseBody(body *SyncImageInternalServerErrorResponseBody) (err error) {
	if body.Code == nil {
		err = goa.MergeErrors(err, goa.MissingFieldError("code", "body"))
	}
	if body.Message == nil {
		err = goa.MergeErrors(err, goa.MissingFieldError("message", "body"))
	}
	return
}
