// Code generated by goa v3.20.0, DO NOT EDIT.
//
// artifact client HTTP transport
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/tibuild/design

package client

import (
	"context"
	"net/http"

	goahttp "goa.design/goa/v3/http"
	goa "goa.design/goa/v3/pkg"
)

// Client lists the artifact service endpoint HTTP clients.
type Client struct {
	// SyncImage Doer is the HTTP client used to make requests to the syncImage
	// endpoint.
	SyncImageDoer goahttp.Doer

	// RestoreResponseBody controls whether the response bodies are reset after
	// decoding so they can be read again.
	RestoreResponseBody bool

	scheme  string
	host    string
	encoder func(*http.Request) goahttp.Encoder
	decoder func(*http.Response) goahttp.Decoder
}

// NewClient instantiates HTTP clients for all the artifact service servers.
func NewClient(
	scheme string,
	host string,
	doer goahttp.Doer,
	enc func(*http.Request) goahttp.Encoder,
	dec func(*http.Response) goahttp.Decoder,
	restoreBody bool,
) *Client {
	return &Client{
		SyncImageDoer:       doer,
		RestoreResponseBody: restoreBody,
		scheme:              scheme,
		host:                host,
		decoder:             dec,
		encoder:             enc,
	}
}

// SyncImage returns an endpoint that makes HTTP requests to the artifact
// service syncImage server.
func (c *Client) SyncImage() goa.Endpoint {
	var (
		encodeRequest  = EncodeSyncImageRequest(c.encoder)
		decodeResponse = DecodeSyncImageResponse(c.decoder, c.RestoreResponseBody)
	)
	return func(ctx context.Context, v any) (any, error) {
		req, err := c.BuildSyncImageRequest(ctx, v)
		if err != nil {
			return nil, err
		}
		err = encodeRequest(req, v)
		if err != nil {
			return nil, err
		}
		resp, err := c.SyncImageDoer.Do(req)
		if err != nil {
			return nil, goahttp.ErrRequestError("artifact", "syncImage", err)
		}
		return decodeResponse(resp)
	}
}
