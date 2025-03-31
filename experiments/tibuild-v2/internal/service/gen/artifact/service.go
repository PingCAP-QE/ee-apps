// Code generated by goa v3.20.0, DO NOT EDIT.
//
// artifact service
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/tibuild/internal/service/design -o
// ./service

package artifact

import (
	"context"
)

// The artifact service provides operations to manage artifacts.
type Service interface {
	// Sync hotfix image to dockerhub
	SyncImage(context.Context, *ImageSyncRequest) (res *ImageSyncRequest, err error)
}

// APIName is the name of the API as defined in the design.
const APIName = "tibuild"

// APIVersion is the version of the API as defined in the design.
const APIVersion = "2.0.0"

// ServiceName is the name of the service as defined in the design. This is the
// same value that is set in the endpoint request contexts under the ServiceKey
// key.
const ServiceName = "artifact"

// MethodNames lists the service method names as defined in the design. These
// are the same values that are set in the endpoint request contexts under the
// MethodKey key.
var MethodNames = [1]string{"syncImage"}

type HTTPError struct {
	Code    int
	Message string
}

// ImageSyncRequest is the payload type of the artifact service syncImage
// method.
type ImageSyncRequest struct {
	Source string
	Target string
}

// Error returns an error description.
func (e *HTTPError) Error() string {
	return ""
}

// ErrorName returns "HTTPError".
//
// Deprecated: Use GoaErrorName - https://github.com/goadesign/goa/issues/3105
func (e *HTTPError) ErrorName() string {
	return e.GoaErrorName()
}

// GoaErrorName returns "HTTPError".
func (e *HTTPError) GoaErrorName() string {
	return "BadRequest"
}
