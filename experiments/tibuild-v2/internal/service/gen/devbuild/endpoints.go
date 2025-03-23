// Code generated by goa v3.20.0, DO NOT EDIT.
//
// devbuild endpoints
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/tibuild/internal/service/design -o
// ./service

package devbuild

import (
	"context"

	goa "goa.design/goa/v3/pkg"
)

// Endpoints wraps the "devbuild" service endpoints.
type Endpoints struct {
	List        goa.Endpoint
	Create      goa.Endpoint
	Get         goa.Endpoint
	Update      goa.Endpoint
	Rerun       goa.Endpoint
	IngestEvent goa.Endpoint
}

// NewEndpoints wraps the methods of the "devbuild" service with endpoints.
func NewEndpoints(s Service) *Endpoints {
	return &Endpoints{
		List:        NewListEndpoint(s),
		Create:      NewCreateEndpoint(s),
		Get:         NewGetEndpoint(s),
		Update:      NewUpdateEndpoint(s),
		Rerun:       NewRerunEndpoint(s),
		IngestEvent: NewIngestEventEndpoint(s),
	}
}

// Use applies the given middleware to all the "devbuild" service endpoints.
func (e *Endpoints) Use(m func(goa.Endpoint) goa.Endpoint) {
	e.List = m(e.List)
	e.Create = m(e.Create)
	e.Get = m(e.Get)
	e.Update = m(e.Update)
	e.Rerun = m(e.Rerun)
	e.IngestEvent = m(e.IngestEvent)
}

// NewListEndpoint returns an endpoint function that calls the method "list" of
// service "devbuild".
func NewListEndpoint(s Service) goa.Endpoint {
	return func(ctx context.Context, req any) (any, error) {
		p := req.(*ListPayload)
		return s.List(ctx, p)
	}
}

// NewCreateEndpoint returns an endpoint function that calls the method
// "create" of service "devbuild".
func NewCreateEndpoint(s Service) goa.Endpoint {
	return func(ctx context.Context, req any) (any, error) {
		p := req.(*CreatePayload)
		return s.Create(ctx, p)
	}
}

// NewGetEndpoint returns an endpoint function that calls the method "get" of
// service "devbuild".
func NewGetEndpoint(s Service) goa.Endpoint {
	return func(ctx context.Context, req any) (any, error) {
		p := req.(*GetPayload)
		return s.Get(ctx, p)
	}
}

// NewUpdateEndpoint returns an endpoint function that calls the method
// "update" of service "devbuild".
func NewUpdateEndpoint(s Service) goa.Endpoint {
	return func(ctx context.Context, req any) (any, error) {
		p := req.(*UpdatePayload)
		return s.Update(ctx, p)
	}
}

// NewRerunEndpoint returns an endpoint function that calls the method "rerun"
// of service "devbuild".
func NewRerunEndpoint(s Service) goa.Endpoint {
	return func(ctx context.Context, req any) (any, error) {
		p := req.(*RerunPayload)
		return s.Rerun(ctx, p)
	}
}

// NewIngestEventEndpoint returns an endpoint function that calls the method
// "ingestEvent" of service "devbuild".
func NewIngestEventEndpoint(s Service) goa.Endpoint {
	return func(ctx context.Context, req any) (any, error) {
		p := req.(*CloudEventIngestEventPayload)
		return s.IngestEvent(ctx, p)
	}
}
