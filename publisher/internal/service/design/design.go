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

var FromOci = Type("FromOci", func() {
	Description("Source from an OCI artifact")
	Attribute("repo", String, func() {
		Example("hub.pingcap.net/dev/ci/tidb")
		Meta("struct:tag:json", "repo,omitempty")
	})
	Attribute("tag", String, func() {
		Example("v7.5.0_linux_amd64")
		Meta("struct:tag:json", "tag,omitempty")
	})
	Attribute("file", String, func() {
		Example("tidb-v7.5.0-linux-amd64.tar.gz")
		Meta("struct:tag:json", "file,omitempty")
	})
	Required("repo", "tag", "file")
	Example(map[string]any{"repo": "hub.pingcap.net/dev/ci/tidb", "tag": "v7.5.0_linux_amd64", "file": "tidb-v7.5.0-linux-amd64.tar.gz"})
})

var FromHTTP = Type("FromHTTP", func() {
	Description("Source from a direct HTTP URL")
	Attribute("url", String, func() {
		Example("https://example.com/tidb-v7.5.0-linux-amd64.tar.gz")
		Meta("struct:tag:json", "url,omitempty")
	})
	Required("url")
	Example(map[string]any{"url": "https://example.com/tidb-v7.5.0-linux-amd64.tar.gz"})
})

var From = Type("From", func() {
	Attribute("type", String, func() {
		Enum("oci", "http")
		Meta("struct:tag:json", "type,omitempty")
	})
	Attribute("oci", FromOci, func() {
		Meta("struct:tag:json", "oci,omitempty")
	})
	Attribute("http", FromHTTP, func() {
		Meta("struct:tag:json", "http,omitempty")
	})
	Required("type")
	Example(map[string]any{"type": "http", "http": map[string]any{"url": "https://example.com/tidb-v7.5.0-linux-amd64.tar.gz"}})
})

var PublishInfoTiUP = Type("PublishInfoTiUP", func() {
	Attribute("name", String, func() {
		Example("tidb")
		Meta("struct:tag:json", "name,omitempty")
	})
	Attribute("os", String, func() {
		Example("linux")
		Meta("struct:tag:json", "os,omitempty")
	})
	Attribute("arch", String, func() {
		Example("amd64")
		Meta("struct:tag:json", "arch,omitempty")
	})
	Attribute("version", String, func() {
		Example("v7.5.0")
		Meta("struct:tag:json", "version,omitempty")
	})
	Attribute("description", String, func() {
		Example("TiDB GA")
		Meta("struct:tag:json", "description,omitempty")
	})
	Attribute("entry_point", String, func() {
		Example("bin/tidb-server")
		Meta("struct:tag:json", "entry_point,omitempty")
	})
	Attribute("standalone", Boolean, func() {
		Example(false)
		Meta("struct:tag:json", "standalone,omitempty")
	})
	Required("name", "os", "arch", "version")
	Example(map[string]any{"name": "tidb", "os": "linux", "arch": "amd64", "version": "v7.5.0", "description": "TiDB GA", "entry_point": "bin/tidb-server", "standalone": false})
})

var PublishRequestTiUP = Type("PublishRequestTiUP", func() {
	Attribute("from", From, func() {
		Meta("struct:tag:json", "from,omitempty")
	})
	Attribute("publish", PublishInfoTiUP, func() {
		Meta("struct:tag:json", "publish,omitempty")
	})
	Required("from", "publish")
	Example(map[string]any{
		"from": map[string]any{
			"type": "http",
			"http": map[string]any{"url": "https://example.com/tidb-v7.5.0-linux-amd64.tar.gz"},
		},
		"publish": map[string]any{
			"name":        "tidb",
			"os":          "linux",
			"arch":        "amd64",
			"version":     "v7.5.0",
			"description": "TiDB GA",
			"entry_point": "bin/tidb-server",
			"standalone":  false,
		},
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
				Example("https://example.com/artifact.tar.gz")
			})
			Attribute("version", String, func() {
				Description("Force set the version. Default is the artifact version read from `org.opencontainers.image.version` of the manifest config.")
				Example("v1.0.0")
			})
			Attribute("tiup-mirror", String, func() {
				Description("Staging is http://tiup.pingcap.net:8988, product is http://tiup.pingcap.net:8987.")
				Default("http://tiup.pingcap.net:8988")
			})
			Required("artifact_url", "tiup-mirror")
		})
		Result(ArrayOf(String), "request track ids")
		HTTP(func() {
			POST("/publish-request")
			Response(StatusOK)
		})
	})

	// Publish a single TiUP package directly from a binary tarball.
	Method("request-to-publish-single", func() {
		Description("Request to publish a single TiUP package from a binary tarball")
		Payload(PublishRequestTiUP)
		Result(String, "request id", func() {
			Format(FormatUUID)
		})
		HTTP(func() {
			POST("/publish-request-single")
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
})
