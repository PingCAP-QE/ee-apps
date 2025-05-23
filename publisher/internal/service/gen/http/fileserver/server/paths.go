// Code generated by goa v3.19.1, DO NOT EDIT.
//
// HTTP request path constructors for the fileserver service.
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/publisher/internal/service/design -o
// ./service

package server

import (
	"fmt"
)

// RequestToPublishFileserverPath returns the URL path to the fileserver service request-to-publish HTTP endpoint.
func RequestToPublishFileserverPath() string {
	return "/fs/publish-request"
}

// QueryPublishingStatusFileserverPath returns the URL path to the fileserver service query-publishing-status HTTP endpoint.
func QueryPublishingStatusFileserverPath(requestID string) string {
	return fmt.Sprintf("/fs/publish-request/%v", requestID)
}
