// Code generated by goa v3.19.1, DO NOT EDIT.
//
// image HTTP server encoders and decoders
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/publisher/internal/service/design -o
// ./service

package server

import (
	"context"
	"errors"
	"io"
	"net/http"

	goahttp "goa.design/goa/v3/http"
	goa "goa.design/goa/v3/pkg"
)

// EncodeRequestToCopyResponse returns an encoder for responses returned by the
// image request-to-copy endpoint.
func EncodeRequestToCopyResponse(encoder func(context.Context, http.ResponseWriter) goahttp.Encoder) func(context.Context, http.ResponseWriter, any) error {
	return func(ctx context.Context, w http.ResponseWriter, v any) error {
		res, _ := v.(string)
		enc := encoder(ctx, w)
		body := res
		w.WriteHeader(http.StatusOK)
		return enc.Encode(body)
	}
}

// DecodeRequestToCopyRequest returns a decoder for requests sent to the image
// request-to-copy endpoint.
func DecodeRequestToCopyRequest(mux goahttp.Muxer, decoder func(*http.Request) goahttp.Decoder) func(*http.Request) (any, error) {
	return func(r *http.Request) (any, error) {
		var (
			body RequestToCopyRequestBody
			err  error
		)
		err = decoder(r).Decode(&body)
		if err != nil {
			if err == io.EOF {
				return nil, goa.MissingPayloadError()
			}
			var gerr *goa.ServiceError
			if errors.As(err, &gerr) {
				return nil, gerr
			}
			return nil, goa.DecodePayloadError(err.Error())
		}
		err = ValidateRequestToCopyRequestBody(&body)
		if err != nil {
			return nil, err
		}
		payload := NewRequestToCopyPayload(&body)

		return payload, nil
	}
}

// EncodeQueryCopyingStatusResponse returns an encoder for responses returned
// by the image query-copying-status endpoint.
func EncodeQueryCopyingStatusResponse(encoder func(context.Context, http.ResponseWriter) goahttp.Encoder) func(context.Context, http.ResponseWriter, any) error {
	return func(ctx context.Context, w http.ResponseWriter, v any) error {
		res, _ := v.(string)
		enc := encoder(ctx, w)
		body := res
		w.WriteHeader(http.StatusOK)
		return enc.Encode(body)
	}
}

// DecodeQueryCopyingStatusRequest returns a decoder for requests sent to the
// image query-copying-status endpoint.
func DecodeQueryCopyingStatusRequest(mux goahttp.Muxer, decoder func(*http.Request) goahttp.Decoder) func(*http.Request) (any, error) {
	return func(r *http.Request) (any, error) {
		var (
			requestID string
			err       error

			params = mux.Vars(r)
		)
		requestID = params["request_id"]
		err = goa.MergeErrors(err, goa.ValidateFormat("request_id", requestID, goa.FormatUUID))
		if err != nil {
			return nil, err
		}
		payload := NewQueryCopyingStatusPayload(requestID)

		return payload, nil
	}
}
