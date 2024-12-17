// Code generated by goa v3.19.1, DO NOT EDIT.
//
// tiup service
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/publisher/design

package tiup

import (
	"context"
)

// TiUP Publisher service
type Service interface {
	// RequestToPublish implements request-to-publish.
	RequestToPublish(context.Context, *RequestToPublishPayload) (res []string, err error)
	// QueryPublishingStatus implements query-publishing-status.
	QueryPublishingStatus(context.Context, *QueryPublishingStatusPayload) (res string, err error)
	// ResetRateLimit implements reset-rate-limit.
	ResetRateLimit(context.Context) (err error)
}

// APIName is the name of the API as defined in the design.
const APIName = "publisher"

// APIVersion is the version of the API as defined in the design.
const APIVersion = "1.0.0"

// ServiceName is the name of the service as defined in the design. This is the
// same value that is set in the endpoint request contexts under the ServiceKey
// key.
const ServiceName = "tiup"

// MethodNames lists the service method names as defined in the design. These
// are the same values that are set in the endpoint request contexts under the
// MethodKey key.
var MethodNames = [3]string{"request-to-publish", "query-publishing-status", "reset-rate-limit"}

// QueryPublishingStatusPayload is the payload type of the tiup service
// query-publishing-status method.
type QueryPublishingStatusPayload struct {
	// request track id
	RequestID string
}

// RequestToPublishPayload is the payload type of the tiup service
// request-to-publish method.
type RequestToPublishPayload struct {
	// The full url of the pushed OCI artifact, contain the tag part. It will parse
	// the repo from it.
	ArtifactURL string
	// Force set the version. Default is the artifact version read from
	// `org.opencontainers.image.version` of the manifest config.
	Version *string
	// Staging is http://tiup.pingcap.net:8988, product is
	// http://tiup.pingcap.net:8987.
	TiupMirror string
	// The request id
	RequestID *string
}
