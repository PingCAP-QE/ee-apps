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
	Attribute("tiup_mirror", String, TiupMirrorFunc)
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

var TiupMirrorFunc = func() {
	Description("`staging` is http://tiup.pingcap.net:8988, `prod` is http://tiup.pingcap.net:8987.")
	Enum("staging", "prod")
	Default("staging")
	Meta("struct:tag:json", "tiup_mirror,omitempty")
}

var RequestTaskIDFunc = func() {
	Description("Request id for async mode (uuidv4 format)")
	Format(FormatUUID)
}

var TiupMirrorName = Type("TiupMirrorName", String, func() {
	Description("Name of the TiUP mirror")
	Enum("staging", "prod")
})

var TiupDeliveryResults = Type("TiupDeliveryResults", func() {
	Attribute("results", MapOf(TiupMirrorName, ArrayOf(String, RequestTaskIDFunc)))
})

var TidbcloudOpsTicket = Type("TidbcloudOpsTicket", func() {
	Description("Ops ticket details")
	Attribute("id", String, "ticket ID")
	Attribute("url", String, func() {
		Description("ticket visit url")
		Format(FormatURI)
	})
	Attribute("release_id", String, "release window ID")
	Attribute("change_id", String, "component publish flow ID")
	Attribute("component", String, "component name")
	Attribute("component_version", String, "component version derived from image tag")
	Required("id", "url", "component", "component_version")
})

var TaskStateFunc = func() {
	Description("State of the task")
	Enum("queued", "processing", "success", "failed", "canceled")
}

var _ = Service("tiup", func() {
	Description("TiUP Publisher service")
	HTTP(func() {
		Path("/tiup")
	})

	Method("request-to-publish", func() {
		Description("Request to publish TiUP packages from a OCI artifact")
		Payload(func() {
			Attribute("artifact_url", String, func() {
				Description("The full url of the pushed OCI artifact, contain the tag part. It will parse the repo from it.")
				Example("oci.com/repo:tag")
			})
			Attribute("tiup_mirror", String, TiupMirrorFunc)
			Attribute("version", String, func() {
				Description("Force set the version. Default is the artifact version read from `org.opencontainers.image.version` of the manifest config.")
				Example("v1.0.0")
			})
			Required("artifact_url", "tiup_mirror")
		})
		Result(ArrayOf(String), "request track ids")
		HTTP(func() {
			POST("/publish-request")
			Response(StatusOK)
		})
	})

	Method("delivery-by-rules", func() {
		Description("Request to delivery TiUP packages from OCI artifact controlled by delivery rules")
		Payload(func() {
			Attribute("artifact_url", String, func() {
				Description("The full url of the pushed OCI artifact, contain the tag part. It will parse the repo from it.")
				Example("oci.com/repo:tag")
			})
			Required("artifact_url")
		})
		Result(ArrayOf(String), "request track ids")
		HTTP(func() {
			POST("/delivery-by-rules")
			Response(StatusOK)
		})
	})

	// Publish a single TiUP package directly from a binary tarball.
	Method("request-to-publish-single", func() {
		Description("Request to publish a single TiUP package from a binary tarball")
		Payload(PublishRequestTiUP)
		Result(String, RequestTaskIDFunc)
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
		Result(String, TaskStateFunc)
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
		Result(ArrayOf(String, RequestTaskIDFunc), "request track ids")
		HTTP(func() {
			POST("/publish-request")
			Response(StatusOK)
		})
	})
	Method("query-publishing-status", func() {
		Payload(func() {
			Attribute("request_id", String, RequestTaskIDFunc)
			Required("request_id")
		})
		Result(String, TaskStateFunc)
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
		Result(String, RequestTaskIDFunc)
		HTTP(func() {
			POST("/copy")
			Response(StatusOK)
		})
	})
	Method("query-copying-status", func() {
		Payload(func() {
			Attribute("request_id", String, RequestTaskIDFunc)
			Required("request_id")
		})
		Result(String, TaskStateFunc)
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
			Attribute("request_id", String, RequestTaskIDFunc)
			Required("async")
		})
		HTTP(func() {
			POST("/collect-multiarch")
			Response(StatusOK)
		})
	})
	Method("query-multiarch-collect-status", func() {
		Payload(func() {
			Attribute("request_id", String, RequestTaskIDFunc)
			Required("request_id")
		})
		Result(String, TaskStateFunc)
		HTTP(func() {
			GET("/collect-multiarch/{request_id}")
			Response(StatusOK)
		})
	})
})

var _ = Service("tidbcloud", func() {
	Description("Publisher service for tidbcloud platform")
	HTTP(func() {
		Path("/tidbcloud")
	})
	Method("update-component-version-in-cloudconfig", func() {
		Payload(func() {
			Attribute("stage", String, "env stage", func() {
				Example("prod")
			})
			Attribute("image", String, "container image with tag", func() {
				Example("xxx.com/component:v8.5.4")
			})

			Required("stage", "image")
		})
		Result(func() {
			Attribute("stage", String)
			Attribute("tickets", ArrayOf(TidbcloudOpsTicket))
			Required("stage", "tickets")
		})
		HTTP(func() {
			POST("/devops/cloudconfig/versions/component")
			Response(StatusOK)
		})
	})
	Method("add-tidbx-image-tag-in-tcms", func() {
		Payload(func() {
			Attribute("image", String, "container image with tag", func() {
				Example("xxx.com/component:v8.5.4")
			})
			Attribute("github", func() { // Should read config from the image when the attribute is not given.
				Description("git informations")
				Attribute("full_repo", String, "full github repo name", func() {
					Example("pingcap/tidb")
				})
				Attribute("ref", String, "git ref", func() {
					Example("refs/heads/master")
				})
				Attribute("commit_sha", String, "full commit SHA", func() {
					MinLength(40)
					MaxLength(40)
					Example("031069dfc0c70e839d996c9e1cf3d34930fc662f")
				})
				Required("full_repo", "commit_sha")
			})
			Required("image")

		})
		Result(func() {
			Attribute("repo", String, "github full repo", func() {
				Meta("struct:tag:json", "repo,omitempty")
				Example("pingcap/tidb")
			})
			Attribute("branch", String, "github branch or tag name", func() {
				Meta("struct:tag:json", "branch,omitempty")
				Example("release-nextgen-20251011")
			})
			Attribute("sha", String, "github commit sha in the repo", func() {
				Meta("struct:tag:json", "sha,omitempty")
				Example("031069dfc0c70e839d996c9e1cf3d34930fc662f")
			})
			Attribute("imageTag", String, "image tag", func() {
				Meta("struct:tag:json", "imageTag,omitempty")
				Example("release-nextgen-20251011-031069d")
			})
		})
		HTTP(func() {
			POST("/tidbx-component-image-builds")
			Response(StatusOK)
		})
	})
})
