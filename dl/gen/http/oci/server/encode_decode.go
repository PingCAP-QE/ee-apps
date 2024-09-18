// Code generated by goa v3.16.1, DO NOT EDIT.
//
// oci HTTP server encoders and decoders
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/dl/design

package server

import (
	"context"
	"net/http"
	"strconv"

	oci "github.com/PingCAP-QE/ee-apps/dl/gen/oci"
	goahttp "goa.design/goa/v3/http"
	goa "goa.design/goa/v3/pkg"
)

// EncodeListFilesResponse returns an encoder for responses returned by the oci
// list-files endpoint.
func EncodeListFilesResponse(encoder func(context.Context, http.ResponseWriter) goahttp.Encoder) func(context.Context, http.ResponseWriter, any) error {
	return func(ctx context.Context, w http.ResponseWriter, v any) error {
		res, _ := v.([]string)
		enc := encoder(ctx, w)
		body := res
		w.WriteHeader(http.StatusOK)
		return enc.Encode(body)
	}
}

// DecodeListFilesRequest returns a decoder for requests sent to the oci
// list-files endpoint.
func DecodeListFilesRequest(mux goahttp.Muxer, decoder func(*http.Request) goahttp.Decoder) func(*http.Request) (any, error) {
	return func(r *http.Request) (any, error) {
		var (
			repository string
			tag        string
			err        error

			params = mux.Vars(r)
		)
		repository = params["repository"]
		tag = r.URL.Query().Get("tag")
		if tag == "" {
			err = goa.MergeErrors(err, goa.MissingFieldError("tag", "query string"))
		}
		if err != nil {
			return nil, err
		}
		payload := NewListFilesPayload(repository, tag)

		return payload, nil
	}
}

// EncodeDownloadFileResponse returns an encoder for responses returned by the
// oci download-file endpoint.
func EncodeDownloadFileResponse(encoder func(context.Context, http.ResponseWriter) goahttp.Encoder) func(context.Context, http.ResponseWriter, any) error {
	return func(ctx context.Context, w http.ResponseWriter, v any) error {
		res, _ := v.(*oci.DownloadFileResult)
		ctx = context.WithValue(ctx, goahttp.ContentTypeKey, "application/octet-stream")
		{
			val := res.Length
			lengths := strconv.FormatInt(val, 10)
			w.Header().Set("Content-Length", lengths)
		}
		w.Header().Set("Content-Disposition", res.ContentDisposition)
		w.WriteHeader(http.StatusOK)
		return nil
	}
}

// DecodeDownloadFileRequest returns a decoder for requests sent to the oci
// download-file endpoint.
func DecodeDownloadFileRequest(mux goahttp.Muxer, decoder func(*http.Request) goahttp.Decoder) func(*http.Request) (any, error) {
	return func(r *http.Request) (any, error) {
		var (
			repository string
			tag        string
			file       *string
			fileRegex  *string
			err        error

			params = mux.Vars(r)
		)
		repository = params["repository"]
		qp := r.URL.Query()
		tag = qp.Get("tag")
		if tag == "" {
			err = goa.MergeErrors(err, goa.MissingFieldError("tag", "query string"))
		}
		fileRaw := qp.Get("file")
		if fileRaw != "" {
			file = &fileRaw
		}
		fileRegexRaw := qp.Get("file_regex")
		if fileRegexRaw != "" {
			fileRegex = &fileRegexRaw
		}
		if fileRegex != nil {
			err = goa.MergeErrors(err, goa.ValidateFormat("file_regex", *fileRegex, goa.FormatRegexp))
		}
		if err != nil {
			return nil, err
		}
		payload := NewDownloadFilePayload(repository, tag, file, fileRegex)

		return payload, nil
	}
}

// EncodeDownloadFileSha256Response returns an encoder for responses returned
// by the oci download-file-sha256 endpoint.
func EncodeDownloadFileSha256Response(encoder func(context.Context, http.ResponseWriter) goahttp.Encoder) func(context.Context, http.ResponseWriter, any) error {
	return func(ctx context.Context, w http.ResponseWriter, v any) error {
		res, _ := v.(*oci.DownloadFileSha256Result)
		ctx = context.WithValue(ctx, goahttp.ContentTypeKey, "application/plain-text")
		{
			val := res.Length
			lengths := strconv.FormatInt(val, 10)
			w.Header().Set("Content-Length", lengths)
		}
		w.Header().Set("Content-Disposition", res.ContentDisposition)
		w.WriteHeader(http.StatusOK)
		return nil
	}
}

// DecodeDownloadFileSha256Request returns a decoder for requests sent to the
// oci download-file-sha256 endpoint.
func DecodeDownloadFileSha256Request(mux goahttp.Muxer, decoder func(*http.Request) goahttp.Decoder) func(*http.Request) (any, error) {
	return func(r *http.Request) (any, error) {
		var (
			repository string
			file       string
			tag        string
			err        error

			params = mux.Vars(r)
		)
		repository = params["repository"]
		qp := r.URL.Query()
		file = qp.Get("file")
		if file == "" {
			err = goa.MergeErrors(err, goa.MissingFieldError("file", "query string"))
		}
		tag = qp.Get("tag")
		if tag == "" {
			err = goa.MergeErrors(err, goa.MissingFieldError("tag", "query string"))
		}
		if err != nil {
			return nil, err
		}
		payload := NewDownloadFileSha256Payload(repository, file, tag)

		return payload, nil
	}
}
