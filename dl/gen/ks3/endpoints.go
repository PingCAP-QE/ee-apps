// Code generated by goa v3.16.1, DO NOT EDIT.
//
// ks3 endpoints
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/dl/design

package ks3

import (
	"context"
	"io"

	goa "goa.design/goa/v3/pkg"
)

// Endpoints wraps the "ks3" service endpoints.
type Endpoints struct {
	DownloadObject goa.Endpoint
}

// DownloadObjectResponseData holds both the result and the HTTP response body
// reader of the "download-object" method.
type DownloadObjectResponseData struct {
	// Result is the method result.
	Result *DownloadObjectResult
	// Body streams the HTTP response body.
	Body io.ReadCloser
}

// NewEndpoints wraps the methods of the "ks3" service with endpoints.
func NewEndpoints(s Service) *Endpoints {
	return &Endpoints{
		DownloadObject: NewDownloadObjectEndpoint(s),
	}
}

// Use applies the given middleware to all the "ks3" service endpoints.
func (e *Endpoints) Use(m func(goa.Endpoint) goa.Endpoint) {
	e.DownloadObject = m(e.DownloadObject)
}

// NewDownloadObjectEndpoint returns an endpoint function that calls the method
// "download-object" of service "ks3".
func NewDownloadObjectEndpoint(s Service) goa.Endpoint {
	return func(ctx context.Context, req any) (any, error) {
		p := req.(*DownloadObjectPayload)
		res, body, err := s.DownloadObject(ctx, p)
		if err != nil {
			return nil, err
		}
		return &DownloadObjectResponseData{Result: res, Body: body}, nil
	}
}
