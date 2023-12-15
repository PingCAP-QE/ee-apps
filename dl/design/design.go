package design

import (
	. "goa.design/goa/v3/dsl"
)

var _ = API("dl", func() {
	Title("Download OCI artifacts Service")
	Description("Service for downloading files from OCI artifact")
	Server("server", func() {
		Host("localhost", func() {
			URI("http://localhost:8000")
		})
	})
})

var _ = Service("health", func() {
	Description("Health service")

	Method("healthz", func() {
		Result(Boolean)
		HTTP(func() {
			GET("/healthz")
		})
	})

	Method("livez", func() {
		Result(Boolean)
		HTTP(func() {
			GET("/livez")
		})
	})
})

var _ = Service("oci", func() {
	Description("OCI artifacts download service")

	Method("list-files", func() {
		Payload(func() {
			Field(1, "repository", String, "OCI artifact repository")
			Field(2, "tag", String, "OCI artifact tag")
			Required("repository", "tag")
		})

		// The use of Result here illustrates how HTTP headers can still be
		// properly encoded and validated when using SkipResponseBodyEncode. It
		// is not generally required to implement a download method.
		Result(ArrayOf(String))

		HTTP(func() {
			GET("/oci-files/{*repository}")
			Param("tag:tag", String, "OCI artifact tag")
		})
	})

	Method("download-file", func() {
		Payload(func() {
			Field(1, "repository", String, "OCI artifact repository")
			Field(2, "tag", String, "OCI artifact tag")
			Field(3, "file", String, "file name in OCI artifact")
			Required("repository", "tag", "file")
		})

		// The use of Result here illustrates how HTTP headers can still be
		// properly encoded and validated when using SkipResponseBodyEncode. It
		// is not generally required to implement a download method.
		Result(func() {
			Attribute("length", Int64, "Length is the downloaded content length in bytes.", func() {
				Example(4 * 1024 * 1024)
			})
			Attribute("contentDisposition", String, "Content-Disposition header for downloading", func() {
				Example("attachment; filename*=UTF-8''tidb-v7.5.0-darwin-arm64.tar.gz")
			})
			Required("length", "contentDisposition")
		})

		Error("invalid_file_path", ErrorResult, "Could not locate file for download")
		Error("internal_error", ErrorResult, "Fault while processing download.")

		HTTP(func() {
			GET("/oci-file/{*repository}")
			Param("file:file", String, "file name in OCI artifact")
			Param("tag:tag", String, "OCI artifact tag")

			// Bypass response body encoder code generation to alleviate need for
			// loading the entire response body in memory.
			SkipResponseBodyEncodeDecode()

			Response(func() {
				// Set the content type for binary data
				ContentType("application/octet-stream")
				Header("length:Content-Length")
				Header("contentDisposition:Content-Disposition")
			})
		})
	})
})

var _ = Service("ks3", func() {
	Description("OCI artifacts download service")

	Method("download-object", func() {
		Payload(func() {
			Field(1, "bucket", String, "bucket name")
			Field(2, "key", String, "object key")

			Required("bucket", "key")
		})

		// The use of Result here illustrates how HTTP headers can still be
		// properly encoded and validated when using SkipResponseBodyEncode. It
		// is not generally required to implement a download method.
		Result(func() {
			Attribute("length", Int64, "Length is the downloaded content length in bytes.", func() {
				Example(4 * 1024 * 1024)
			})
			Attribute("contentDisposition", String, "Content-Disposition header for downloading", func() {
				Example("attachment; filename*=UTF-8''tidb-v7.5.0-darwin-arm64.tar.gz")
			})
			Required("length", "contentDisposition")
		})

		Error("invalid_file_path", ErrorResult, "Could not locate file for download")
		Error("internal_error", ErrorResult, "Fault while processing download.")

		HTTP(func() {
			GET("/s3-obj/{bucket}/{*key}")

			// Bypass response body encoder code generation to alleviate need for
			// loading the entire response body in memory.
			SkipResponseBodyEncodeDecode()

			Response(func() {
				// Set the content type for binary data
				ContentType("application/octet-stream")
				Header("length:Content-Length")
				Header("contentDisposition:Content-Disposition")
			})
		})
	})
})
