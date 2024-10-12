// You can lean from https://pkg.go.dev/goa.design/goa/v3/dsl
package design

import (
	_ "goa.design/plugins/v3/zerologger"

	. "goa.design/goa/v3/dsl"
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
	Description("The TiPublisher service")
	HTTP(func() {
		Path("/tiup")
	})
	Method("request-to-publish", func() {
		Payload(func() {
			Attribute("artifact_url", String, func() {
				Description("The full url of the pushed image, contain the tag part. It will parse the repo from it.")
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
		Result(String, "request state")
		HTTP(func() {
			GET("/publish-request/{request_id}")
			Response(StatusOK)
		})
	})
})
