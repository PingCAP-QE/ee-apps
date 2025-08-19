// You can lean from https://pkg.go.dev/goa.design/goa/v3/dsl
package design

import (
	_ "goa.design/plugins/v3/zerologger"

	. "goa.design/goa/v3/dsl" //nolint
)

var _ = API("publisher", func() {
	Title("Publish API")
	Description("Publish API")
	Version("1.0.0")
	Contact(func() {
		Name("WuHui Zuo")
		Email("wuhui.zuo@pingcap.com")
		URL("https://github.com/wuhuizuo")
	})
	Server("publisher", func() {
		Host("localhost", func() {
			URI("http://0.0.0.0:80")
		})
	})
})

var _ = Service("tiup", func() {
	Description("TiUP Publisher service")
	HTTP(func() {
		Path("/tiup")
	})
	Method("request-to-publish", func() {
		Payload(func() {
			Attribute("artifact_url", String, func() {
				Description("The full url of the pushed OCI artifact, contain the tag part. It will parse the repo from it.")
			})
			Attribute("version", String, func() {
				Description("Force set the version. Default is the artifact version read from `org.opencontainers.image.version` of the manifest config.")
			})
			Attribute("tiup-mirror", String, func() {
				Description("Staging is http://tiup.pingcap.net:8988, product is http://tiup.pingcap.net:8987.")
				Default("http://tiup.pingcap.net:8988")
			})
			Attribute("request_id", String, func() {
				Description("The request id")
			})
			Required("artifact_url", "tiup-mirror")
		})
		Result(ArrayOf(String), "request track ids")
		HTTP(func() {
			POST("/publish-request")
			Response(StatusOK)
		})
	})
	Method("query-publishing-status", func() {
		Payload(func() {
			Attribute("request_id", String, "request track id")
			Required("request_id")
		})
		Result(String, "request state", func() {
			Enum("queued", "processing", "success", "failed", "canceled")
		})
		HTTP(func() {
			GET("/publish-request/{request_id}")
			Response(StatusOK)
		})
	})
	Method("reset-rate-limit", func() {
		HTTP(func() {
			POST("/reset-rate-limit")
			Response(StatusOK)
		})
	})
})

var _ = Service("fileserver", func() {
	Description("Publisher service for static file server ")
	HTTP(func() {
		Path("/fs")
	})
	Method("request-to-publish", func() {
		Payload(func() {
			Attribute("artifact_url", String, func() {
				Description("The full url of the pushed OCI artifact, contain the tag part. It will parse the repo from it.")
			})
			Required("artifact_url")
		})
		Result(ArrayOf(String), "request track ids")
		HTTP(func() {
			POST("/publish-request")
			Response(StatusOK)
		})
	})
	Method("query-publishing-status", func() {
		Payload(func() {
			Attribute("request_id", String, "request track id")
			Required("request_id")
		})
		Result(String, "request state", func() {
			Enum("queued", "processing", "success", "failed", "canceled")
		})
		HTTP(func() {
			GET("/publish-request/{request_id}")
			Response(StatusOK)
		})
	})
})

var _ = Service("image", func() {
	Description("Publisher service for container image")
	HTTP(func() {
		Path("/image")
	})

	Method("request-to-copy", func() {
		Payload(func() {
			Attribute("source", String, "source image url")
			Attribute("destination", String, "destination image url")
			Required("source", "destination")
		})
		Result(String, "request id", func() {
			Format(FormatUUID)
		})
		HTTP(func() {
			POST("/copy")
			Response(StatusOK)
		})
	})
	Method("query-copying-status", func() {
		Payload(func() {
			Attribute("request_id", String, "request track id", func() {
				Format(FormatUUID)
			})
			Required("request_id")
		})
		Result(String, "request state", func() {
			Enum("queued", "processing", "success", "failed", "canceled")
		})
		HTTP(func() {
			GET("/copy/{request_id}")
			Response(StatusOK)
		})
	})

	Method("request-multiarch-collect", func() {
		Payload(func() {
			Attribute("image_url", String, "The image URL to collect")
			Attribute("release_tag_suffix", String, func() {
				Description("Suffix for the release tag")
				Default("release")
			})
			Attribute("async", Boolean, func() {
				Description("Whether to run the collection asynchronously. If true, returns a request id. If false or omitted, runs synchronously and returns the result directly.")
				Default(false)
			})
			Required("image_url")
		})
		Result(func() {
			Attribute("async", Boolean, func() {
				Description("Whether to run the collection asynchronously. If true, returns a request id. If false or omitted, runs synchronously and returns the result directly.")
				Default(false)
			})
			// If async is true, request_id is required; if false, repo and tags are required.
			Attribute("repo", String, "Repository of the collected image")
			Attribute("tags", ArrayOf(String), "Tags of the collected image")
			Attribute("request_id", String, func() {
				Description("Request id for async mode (uuidv4 format)")
				Format(FormatUUID)
			})
			Required("async")
		})
		HTTP(func() {
			POST("/collect-multiarch")
			Response(StatusOK)
		})
	})
	Method("query-multiarch-collect-status", func() {
		Payload(func() {
			Attribute("request_id", String, "Request track id", func() {
				Format(FormatUUID)
			})
			Required("request_id")
		})
		Result(String, "request state", func() {
			Enum("queued", "processing", "success", "failed", "canceled")
		})
		HTTP(func() {
			GET("/collect-multiarch/{request_id}")
			Response(StatusOK)
		})
	})
})
