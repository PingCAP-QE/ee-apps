// Code generated by goa v3.19.1, DO NOT EDIT.
//
// image service
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/publisher/internal/service/design -o
// ./service

package image

import (
	"context"
)

// Publisher service for container image
type Service interface {
	// RequestToCopy implements request-to-copy.
	RequestToCopy(context.Context, *RequestToCopyPayload) (res string, err error)
	// QueryCopyingStatus implements query-copying-status.
	QueryCopyingStatus(context.Context, *QueryCopyingStatusPayload) (res string, err error)
}

// APIName is the name of the API as defined in the design.
const APIName = "publisher"

// APIVersion is the version of the API as defined in the design.
const APIVersion = "1.0.0"

// ServiceName is the name of the service as defined in the design. This is the
// same value that is set in the endpoint request contexts under the ServiceKey
// key.
const ServiceName = "image"

// MethodNames lists the service method names as defined in the design. These
// are the same values that are set in the endpoint request contexts under the
// MethodKey key.
var MethodNames = [2]string{"request-to-copy", "query-copying-status"}

// QueryCopyingStatusPayload is the payload type of the image service
// query-copying-status method.
type QueryCopyingStatusPayload struct {
	// request track id
	RequestID string
}

// RequestToCopyPayload is the payload type of the image service
// request-to-copy method.
type RequestToCopyPayload struct {
	// source image url
	Source string
	// destination image url
	Destination string
}
