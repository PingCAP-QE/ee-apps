package design

import (
	. "goa.design/goa/v3/dsl"
	_ "goa.design/plugins/v3/zerologger"
)

var _ = API("tibuild", func() {
	Title("TiBuild API")
	Description("TiBuild API")
	Version("2.0.0")
	Contact(func() {
		Name("Flare Zuo")
		Email("wuhui.zuo@pingcap.com")
		URL("https://github.com/wuhuizuo")
	})
	Server("tibuild", func() {
		Host("development", func() {
			URI("http://localhost:8080")
		})
		Host("product", func() {
			URI("http://0.0.0.0:8080")
		})
	})
})

var _ = Service("artifact", func() {
	Description("The artifact service provides operations to manage artifacts.")
	Error("BadRequest", HTTPError, "Bad Request")
	Error("InternalServerError", HTTPError, "Internal Server Error")
	Method("syncImage", func() {
		Description("Sync hotfix image to dockerhub")
		Payload(func() {
			Attribute("ImageSyncRequest", ImageSyncRequest, "Image sync to public, only hotfix is accepted right now")
			Required("ImageSyncRequest")
		})
		Result(ImageSyncRequest)

		HTTP(func() {
			POST("/api/artifact/sync-image")
			Response(StatusOK)
			Response("BadRequest", StatusBadRequest)
			Response("InternalServerError", StatusInternalServerError)
		})
	})
})

var _ = Service("devbuild", func() {
	Description("The devbuild service provides operations to manage dev builds.")
	Error("BadRequest", HTTPError, "Bad Request")
	Error("InternalServerError", HTTPError, "Internal Server Error")
	Method("list", func() {
		Description("List devbuild")
		Payload(func() {
			Attribute("size", Int, "The size limit of items", func() {
				Default(10)
			})
			Attribute("offset", Int, "The start position of items", func() {
				Default(0)
			})
			Attribute("hotfix", Boolean, "Filter hotfix")
			Attribute("createdBy", String, "Filter created by")
		})
		Result(ArrayOf(DevBuild))
		HTTP(func() {
			GET("/api/devbuilds")
			Param("size")
			Param("offset")
			Param("hotfix")
			Param("createdBy")
			Response(StatusOK)
			Response("BadRequest", StatusBadRequest)
		})
	})

	Method("create", func() {
		Description("Create and trigger devbuild")
		Payload(func() {
			Attribute("DevBuild", DevBuild, "Build to create, only spec field is required, others are ignored")
			Attribute("dryrun", Boolean, "Dry run", func() {
				Default(false)
			})
			Required("DevBuild")
		})
		Result(DevBuild)
		HTTP(func() {
			POST("/api/devbuilds")
			Param("dryrun")
			Response(StatusOK)
			Response("BadRequest", StatusBadRequest)
			Response("InternalServerError", StatusInternalServerError)
		})
	})

	Method("get", func() {
		Description("Get devbuild")
		Payload(func() {
			Attribute("id", Int, "ID of build", func() {
				Example(1)
			})
			Attribute("sync", Boolean, "Whether sync with jenkins", func() {
				Default(false)
			})
			Required("id")
		})
		Result(DevBuild)
		Error("http_error", HTTPError, "Bad Request")
		HTTP(func() {
			GET("/api/devbuilds/{id}")
			Param("sync")
			Response(StatusOK)
			Response("BadRequest", StatusBadRequest)
			Response("InternalServerError", StatusInternalServerError)
		})
	})

	Method("update", func() {
		Description("Update devbuild status")
		Payload(func() {
			Attribute("id", Int, "ID of build", func() {
				Example(1)
			})
			Attribute("DevBuild", DevBuild, "Build to update")
			Attribute("dryrun", Boolean, "Dry run", func() {
				Default(false)
			})
			Required("id", "DevBuild")
		})
		Result(DevBuild)
		HTTP(func() {
			PUT("/api/devbuilds/{id}")
			Param("dryrun")
			Response(StatusOK)
			Response("BadRequest", StatusBadRequest)
			Response("InternalServerError", StatusInternalServerError)
		})
	})

	Method("rerun", func() {
		Description("Rerun devbuild")
		Payload(func() {
			Attribute("id", Int, "ID of build", func() {
				Example(1)
			})
			Attribute("dryrun", Boolean, "Dry run", func() {
				Default(false)
			})
			Required("id")
		})
		Result(DevBuild)
		HTTP(func() {
			POST("/api/devbuilds/{id}/rerun")
			Param("dryrun")
			Response(StatusOK)
			Response("BadRequest", StatusBadRequest)
			Response("InternalServerError", StatusInternalServerError)
		})
	})
})

var ImageSyncRequest = Type("ImageSyncRequest", func() {
	Attribute("source", String)
	Attribute("target", String)
	Required("source", "target")
})

var DevBuild = Type("DevBuild", func() {
	Attribute("id", Int)
	Attribute("meta", DevBuildMeta)
	Attribute("spec", DevBuildSpec)
	Attribute("status", DevBuildStatus)
	Required("id", "meta", "spec", "status")
})

var DevBuildMeta = Type("DevBuildMeta", func() {
	Attribute("createdAt", String)
	Attribute("createdBy", String)
	Attribute("updatedAt", String)
	Required("createdAt", "createdBy", "updatedAt")
})

var DevBuildSpec = Type("DevBuildSpec", func() {
	Attribute("buildEnv", String)
	Attribute("builderImg", String)
	Attribute("edition", ProductEdition)
	Attribute("features", String)
	Attribute("gitHash", String)
	Attribute("gitRef", String)
	Attribute("githubRepo", String)
	Attribute("isHotfix", Boolean)
	Attribute("isPushGCR", Boolean)
	Attribute("pipelineEngine", PipelineEngine)
	Attribute("pluginGitRef", String)
	Attribute("product", Product)
	Attribute("productBaseImg", String)
	Attribute("productDockerfile", String)
	Attribute("targetImg", String)
	Attribute("version", String)
	Required("buildEnv", "builderImg", "edition", "features", "gitHash", "gitRef", "githubRepo", "isHotfix", "isPushGCR", "pipelineEngine", "pluginGitRef", "product", "productBaseImg", "productDockerfile", "targetImg", "version")
})

var DevBuildStatus = Type("DevBuildStatus", func() {
	Attribute("buildReport", BuildReport)
	Attribute("errMsg", String)
	Attribute("pipelineBuildID", Int)
	Attribute("pipelineEndAt", String)
	Attribute("pipelineStartAt", String)
	Attribute("pipelineViewURL", String)
	Attribute("pipelineViewURLs", ArrayOf(String))
	Attribute("status", BuildStatus)
	Attribute("tektonStatus", TektonStatus)
	Required("buildReport", "errMsg", "pipelineBuildID", "pipelineEndAt", "pipelineStartAt", "pipelineViewURL", "pipelineViewURLs", "status", "tektonStatus")
})

var BuildReport = Type("BuildReport", func() {
	Attribute("binaries", ArrayOf(BinArtifact))
	Attribute("gitHash", String)
	Attribute("images", ArrayOf(ImageArtifact))
	Attribute("pluginGitHash", String)
	Attribute("printedVersion", String)
	Required("binaries", "gitHash", "images", "pluginGitHash", "printedVersion")
})

var BinArtifact = Type("BinArtifact", func() {
	Attribute("component", String)
	Attribute("ociFile", OciFile)
	Attribute("platform", String)
	Attribute("sha256OciFile", OciFile)
	Attribute("sha256URL", String)
	Attribute("url", String)
	Required("component", "ociFile", "platform", "sha256OciFile", "sha256URL", "url")
})

var OciFile = Type("OciFile", func() {
	Attribute("file", String)
	Attribute("repo", String)
	Attribute("tag", String)
	Required("file", "repo", "tag")
})

var ImageArtifact = Type("ImageArtifact", func() {
	Attribute("platform", String)
	Attribute("url", String)
	Required("platform", "url")
})

var TektonStatus = Type("TektonStatus", func() {
	Attribute("pipelines", ArrayOf(TektonPipeline))
	Required("pipelines")
})

var TektonPipeline = Type("TektonPipeline", func() {
	Attribute("endAt", String)
	Attribute("gitHash", String)
	Attribute("images", ArrayOf(ImageArtifact))
	Attribute("name", String)
	Attribute("ociArtifacts", ArrayOf(OciArtifact))
	Attribute("platform", String)
	Attribute("startAt", String)
	Attribute("status", BuildStatus)
	Attribute("url", String)
	Required("endAt", "gitHash", "images", "name", "ociArtifacts", "platform", "startAt", "status", "url")
})

var OciArtifact = Type("OciArtifact", func() {
	Attribute("files", ArrayOf(String))
	Attribute("repo", String)
	Attribute("tag", String)
	Required("files", "repo", "tag")
})

var BuildStatus = Type("BuildStatus", String, func() {
	Enum("PENDING", "PROCESSING", "ABORTED", "SUCCESS", "FAILURE", "ERROR")
})

var PipelineEngine = Type("PipelineEngine", String, func() {
	Enum("jenkins", "tekton")
})

var Product = Type("Product", String, func() {
	Enum("tidb", "enterprise-plugin", "tikv", "pd", "tiflash", "br", "dumpling", "tidb-lightning", "ticdc", "ticdc-newarch", "dm", "tidb-binlog", "tidb-tools", "ng-monitoring", "tidb-dashboard", "drainer", "pump", "")
})

var ProductEdition = Type("ProductEdition", String, func() {
	Enum("enterprise", "community")
})

var HTTPError = Type("HTTPError", func() {
	Attribute("code", Int)
	Attribute("message", String)
	Required("code", "message")
})
