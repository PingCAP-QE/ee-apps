// Code generated by goa v3.16.1, DO NOT EDIT.
//
// health HTTP client encoders and decoders
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/dl/design

package client

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/url"

	goahttp "goa.design/goa/v3/http"
)

// BuildHealthzRequest instantiates a HTTP request object with method and path
// set to call the "health" service "healthz" endpoint
func (c *Client) BuildHealthzRequest(ctx context.Context, v any) (*http.Request, error) {
	u := &url.URL{Scheme: c.scheme, Host: c.host, Path: HealthzHealthPath()}
	req, err := http.NewRequest("GET", u.String(), nil)
	if err != nil {
		return nil, goahttp.ErrInvalidURL("health", "healthz", u.String(), err)
	}
	if ctx != nil {
		req = req.WithContext(ctx)
	}

	return req, nil
}

// DecodeHealthzResponse returns a decoder for responses returned by the health
// healthz endpoint. restoreBody controls whether the response body should be
// restored after having been read.
func DecodeHealthzResponse(decoder func(*http.Response) goahttp.Decoder, restoreBody bool) func(*http.Response) (any, error) {
	return func(resp *http.Response) (any, error) {
		if restoreBody {
			b, err := io.ReadAll(resp.Body)
			if err != nil {
				return nil, err
			}
			resp.Body = io.NopCloser(bytes.NewBuffer(b))
			defer func() {
				resp.Body = io.NopCloser(bytes.NewBuffer(b))
			}()
		} else {
			defer resp.Body.Close()
		}
		switch resp.StatusCode {
		case http.StatusOK:
			var (
				body bool
				err  error
			)
			err = decoder(resp).Decode(&body)
			if err != nil {
				return nil, goahttp.ErrDecodingError("health", "healthz", err)
			}
			return body, nil
		default:
			body, _ := io.ReadAll(resp.Body)
			return nil, goahttp.ErrInvalidResponse("health", "healthz", resp.StatusCode, string(body))
		}
	}
}

// BuildLivezRequest instantiates a HTTP request object with method and path
// set to call the "health" service "livez" endpoint
func (c *Client) BuildLivezRequest(ctx context.Context, v any) (*http.Request, error) {
	u := &url.URL{Scheme: c.scheme, Host: c.host, Path: LivezHealthPath()}
	req, err := http.NewRequest("GET", u.String(), nil)
	if err != nil {
		return nil, goahttp.ErrInvalidURL("health", "livez", u.String(), err)
	}
	if ctx != nil {
		req = req.WithContext(ctx)
	}

	return req, nil
}

// DecodeLivezResponse returns a decoder for responses returned by the health
// livez endpoint. restoreBody controls whether the response body should be
// restored after having been read.
func DecodeLivezResponse(decoder func(*http.Response) goahttp.Decoder, restoreBody bool) func(*http.Response) (any, error) {
	return func(resp *http.Response) (any, error) {
		if restoreBody {
			b, err := io.ReadAll(resp.Body)
			if err != nil {
				return nil, err
			}
			resp.Body = io.NopCloser(bytes.NewBuffer(b))
			defer func() {
				resp.Body = io.NopCloser(bytes.NewBuffer(b))
			}()
		} else {
			defer resp.Body.Close()
		}
		switch resp.StatusCode {
		case http.StatusOK:
			var (
				body bool
				err  error
			)
			err = decoder(resp).Decode(&body)
			if err != nil {
				return nil, goahttp.ErrDecodingError("health", "livez", err)
			}
			return body, nil
		default:
			body, _ := io.ReadAll(resp.Body)
			return nil, goahttp.ErrInvalidResponse("health", "livez", resp.StatusCode, string(body))
		}
	}
}
