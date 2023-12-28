// Code generated by goa v3.14.1, DO NOT EDIT.
//
// oci HTTP client encoders and decoders
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
	"strconv"

	oci "github.com/PingCAP-QE/ee-apps/dl/gen/oci"
	goahttp "goa.design/goa/v3/http"
	goa "goa.design/goa/v3/pkg"
)

// BuildListFilesRequest instantiates a HTTP request object with method and
// path set to call the "oci" service "list-files" endpoint
func (c *Client) BuildListFilesRequest(ctx context.Context, v any) (*http.Request, error) {
	var (
		repository string
	)
	{
		p, ok := v.(*oci.ListFilesPayload)
		if !ok {
			return nil, goahttp.ErrInvalidType("oci", "list-files", "*oci.ListFilesPayload", v)
		}
		repository = p.Repository
	}
	u := &url.URL{Scheme: c.scheme, Host: c.host, Path: ListFilesOciPath(repository)}
	req, err := http.NewRequest("GET", u.String(), nil)
	if err != nil {
		return nil, goahttp.ErrInvalidURL("oci", "list-files", u.String(), err)
	}
	if ctx != nil {
		req = req.WithContext(ctx)
	}

	return req, nil
}

// EncodeListFilesRequest returns an encoder for requests sent to the oci
// list-files server.
func EncodeListFilesRequest(encoder func(*http.Request) goahttp.Encoder) func(*http.Request, any) error {
	return func(req *http.Request, v any) error {
		p, ok := v.(*oci.ListFilesPayload)
		if !ok {
			return goahttp.ErrInvalidType("oci", "list-files", "*oci.ListFilesPayload", v)
		}
		values := req.URL.Query()
		values.Add("tag", p.Tag)
		req.URL.RawQuery = values.Encode()
		return nil
	}
}

// DecodeListFilesResponse returns a decoder for responses returned by the oci
// list-files endpoint. restoreBody controls whether the response body should
// be restored after having been read.
func DecodeListFilesResponse(decoder func(*http.Response) goahttp.Decoder, restoreBody bool) func(*http.Response) (any, error) {
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
				body []string
				err  error
			)
			err = decoder(resp).Decode(&body)
			if err != nil {
				return nil, goahttp.ErrDecodingError("oci", "list-files", err)
			}
			return body, nil
		default:
			body, _ := io.ReadAll(resp.Body)
			return nil, goahttp.ErrInvalidResponse("oci", "list-files", resp.StatusCode, string(body))
		}
	}
}

// BuildDownloadFileRequest instantiates a HTTP request object with method and
// path set to call the "oci" service "download-file" endpoint
func (c *Client) BuildDownloadFileRequest(ctx context.Context, v any) (*http.Request, error) {
	var (
		repository string
	)
	{
		p, ok := v.(*oci.DownloadFilePayload)
		if !ok {
			return nil, goahttp.ErrInvalidType("oci", "download-file", "*oci.DownloadFilePayload", v)
		}
		repository = p.Repository
	}
	u := &url.URL{Scheme: c.scheme, Host: c.host, Path: DownloadFileOciPath(repository)}
	req, err := http.NewRequest("GET", u.String(), nil)
	if err != nil {
		return nil, goahttp.ErrInvalidURL("oci", "download-file", u.String(), err)
	}
	if ctx != nil {
		req = req.WithContext(ctx)
	}

	return req, nil
}

// EncodeDownloadFileRequest returns an encoder for requests sent to the oci
// download-file server.
func EncodeDownloadFileRequest(encoder func(*http.Request) goahttp.Encoder) func(*http.Request, any) error {
	return func(req *http.Request, v any) error {
		p, ok := v.(*oci.DownloadFilePayload)
		if !ok {
			return goahttp.ErrInvalidType("oci", "download-file", "*oci.DownloadFilePayload", v)
		}
		values := req.URL.Query()
		values.Add("file", p.File)
		values.Add("tag", p.Tag)
		req.URL.RawQuery = values.Encode()
		return nil
	}
}

// DecodeDownloadFileResponse returns a decoder for responses returned by the
// oci download-file endpoint. restoreBody controls whether the response body
// should be restored after having been read.
func DecodeDownloadFileResponse(decoder func(*http.Response) goahttp.Decoder, restoreBody bool) func(*http.Response) (any, error) {
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
		}
		switch resp.StatusCode {
		case http.StatusOK:
			var (
				length             int64
				contentDisposition string
				err                error
			)
			{
				lengthRaw := resp.Header.Get("Content-Length")
				if lengthRaw == "" {
					return nil, goahttp.ErrValidationError("oci", "download-file", goa.MissingFieldError("length", "header"))
				}
				v, err2 := strconv.ParseInt(lengthRaw, 10, 64)
				if err2 != nil {
					err = goa.MergeErrors(err, goa.InvalidFieldTypeError("length", lengthRaw, "integer"))
				}
				length = v
			}
			contentDispositionRaw := resp.Header.Get("Content-Disposition")
			if contentDispositionRaw == "" {
				err = goa.MergeErrors(err, goa.MissingFieldError("contentDisposition", "header"))
			}
			contentDisposition = contentDispositionRaw
			if err != nil {
				return nil, goahttp.ErrValidationError("oci", "download-file", err)
			}
			res := NewDownloadFileResultOK(length, contentDisposition)
			return res, nil
		default:
			body, _ := io.ReadAll(resp.Body)
			return nil, goahttp.ErrInvalidResponse("oci", "download-file", resp.StatusCode, string(body))
		}
	}
}
