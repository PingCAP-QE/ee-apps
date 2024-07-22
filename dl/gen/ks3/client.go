// Code generated by goa v3.16.1, DO NOT EDIT.
//
// ks3 client
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/dl/design

package ks3

import (
	"context"
	"io"

	goa "goa.design/goa/v3/pkg"
)

// Client is the "ks3" service client.
type Client struct {
	DownloadObjectEndpoint goa.Endpoint
}

// NewClient initializes a "ks3" service client given the endpoints.
func NewClient(downloadObject goa.Endpoint) *Client {
	return &Client{
		DownloadObjectEndpoint: downloadObject,
	}
}

// DownloadObject calls the "download-object" endpoint of the "ks3" service.
// DownloadObject may return the following errors:
//   - "invalid_file_path" (type *goa.ServiceError): Could not locate file for download
//   - "internal_error" (type *goa.ServiceError): Fault while processing download.
//   - error: internal error
func (c *Client) DownloadObject(ctx context.Context, p *DownloadObjectPayload) (res *DownloadObjectResult, resp io.ReadCloser, err error) {
	var ires any
	ires, err = c.DownloadObjectEndpoint(ctx, p)
	if err != nil {
		return
	}
	o := ires.(*DownloadObjectResponseData)
	return o.Result, o.Body, nil
}
