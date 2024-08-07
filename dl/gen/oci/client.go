// Code generated by goa v3.16.1, DO NOT EDIT.
//
// oci client
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/dl/design

package oci

import (
	"context"
	"io"

	goa "goa.design/goa/v3/pkg"
)

// Client is the "oci" service client.
type Client struct {
	ListFilesEndpoint          goa.Endpoint
	DownloadFileEndpoint       goa.Endpoint
	DownloadFileSha256Endpoint goa.Endpoint
}

// NewClient initializes a "oci" service client given the endpoints.
func NewClient(listFiles, downloadFile, downloadFileSha256 goa.Endpoint) *Client {
	return &Client{
		ListFilesEndpoint:          listFiles,
		DownloadFileEndpoint:       downloadFile,
		DownloadFileSha256Endpoint: downloadFileSha256,
	}
}

// ListFiles calls the "list-files" endpoint of the "oci" service.
func (c *Client) ListFiles(ctx context.Context, p *ListFilesPayload) (res []string, err error) {
	var ires any
	ires, err = c.ListFilesEndpoint(ctx, p)
	if err != nil {
		return
	}
	return ires.([]string), nil
}

// DownloadFile calls the "download-file" endpoint of the "oci" service.
// DownloadFile may return the following errors:
//   - "invalid_file_path" (type *goa.ServiceError): Could not locate file for download
//   - "internal_error" (type *goa.ServiceError): Fault while processing download.
//   - error: internal error
func (c *Client) DownloadFile(ctx context.Context, p *DownloadFilePayload) (res *DownloadFileResult, resp io.ReadCloser, err error) {
	var ires any
	ires, err = c.DownloadFileEndpoint(ctx, p)
	if err != nil {
		return
	}
	o := ires.(*DownloadFileResponseData)
	return o.Result, o.Body, nil
}

// DownloadFileSha256 calls the "download-file-sha256" endpoint of the "oci"
// service.
// DownloadFileSha256 may return the following errors:
//   - "invalid_file_path" (type *goa.ServiceError): Could not locate file for download
//   - "internal_error" (type *goa.ServiceError): Fault while processing download.
//   - error: internal error
func (c *Client) DownloadFileSha256(ctx context.Context, p *DownloadFileSha256Payload) (res *DownloadFileSha256Result, resp io.ReadCloser, err error) {
	var ires any
	ires, err = c.DownloadFileSha256Endpoint(ctx, p)
	if err != nil {
		return
	}
	o := ires.(*DownloadFileSha256ResponseData)
	return o.Result, o.Body, nil
}
