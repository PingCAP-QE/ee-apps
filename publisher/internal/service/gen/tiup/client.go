// Code generated by goa v3.19.1, DO NOT EDIT.
//
// tiup client
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/publisher/internal/service/design -o
// ./service

package tiup

import (
	"context"

	goa "goa.design/goa/v3/pkg"
)

// Client is the "tiup" service client.
type Client struct {
	RequestToPublishEndpoint      goa.Endpoint
	QueryPublishingStatusEndpoint goa.Endpoint
	ResetRateLimitEndpoint        goa.Endpoint
}

// NewClient initializes a "tiup" service client given the endpoints.
func NewClient(requestToPublish, queryPublishingStatus, resetRateLimit goa.Endpoint) *Client {
	return &Client{
		RequestToPublishEndpoint:      requestToPublish,
		QueryPublishingStatusEndpoint: queryPublishingStatus,
		ResetRateLimitEndpoint:        resetRateLimit,
	}
}

// RequestToPublish calls the "request-to-publish" endpoint of the "tiup"
// service.
func (c *Client) RequestToPublish(ctx context.Context, p *RequestToPublishPayload) (res []string, err error) {
	var ires any
	ires, err = c.RequestToPublishEndpoint(ctx, p)
	if err != nil {
		return
	}
	return ires.([]string), nil
}

// QueryPublishingStatus calls the "query-publishing-status" endpoint of the
// "tiup" service.
func (c *Client) QueryPublishingStatus(ctx context.Context, p *QueryPublishingStatusPayload) (res string, err error) {
	var ires any
	ires, err = c.QueryPublishingStatusEndpoint(ctx, p)
	if err != nil {
		return
	}
	return ires.(string), nil
}

// ResetRateLimit calls the "reset-rate-limit" endpoint of the "tiup" service.
func (c *Client) ResetRateLimit(ctx context.Context) (err error) {
	_, err = c.ResetRateLimitEndpoint(ctx, nil)
	return
}
