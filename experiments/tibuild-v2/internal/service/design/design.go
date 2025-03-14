package api

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

	HTTP(func() {
		// Add prefix to all API paths
		Path("/api/v2")
	})
})

var _ = Service("artifact", func() {
	Description("The artifact service provides operations to manage artifacts.")
	Error("BadRequest", HTTPError, "Bad Request")
	Error("InternalServerError", HTTPError, "Internal Server Error")
	HTTP(func() {
		Path("/artifact")
	})
	Method("syncImage", func() {
		Description("Sync hotfix image to dockerhub")
		Payload(ImageSyncRequest)
		Result(ImageSyncRequest)

		HTTP(func() {
			POST("/sync-image")
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
	HTTP(func() {
		Path("/devbuilds")
	})
	Method("list", func() {
		Description("List devbuild with pagination support")
		Payload(func() {
			Attribute("page", Int, "The page number of items", func() {
				Default(1)
			})
			Attribute("page_size", Int, "Page size", func() {
				Default(30)
			})
			Attribute("hotfix", Boolean, "Filter hotfix", func() {
				Default(false)
			})
			Attribute("sort", String, "What to sort results by", func() {
				Enum("created_at", "updated_at")
				Default("created_at")
			})
			Attribute("direction", String, "The direction of the sort", func() {
				Enum("asc", "desc")
				Default("desc")
			})
			Attribute("created_by", String, "Filter created by")
		})
		Result(ArrayOf(DevBuild), "List of dev builds")
		HTTP(func() {
			GET("/")
			Param("page")
			Param("page_size")
			Param("hotfix")
			Param("sort")
			Param("created_by")
			Response(StatusOK)
			Response("BadRequest", StatusBadRequest)
		})
	})

	Method("create", func() {
		Description("Create and trigger devbuild")
		Payload(func() {
			Attribute("created_by", String, "Creator of build", func() {
				Format(FormatEmail)
			})
			Attribute("request", DevBuildRequest, "Build to create, only spec field is required, others are ignored")
			Attribute("dryrun", Boolean, "Dry run", func() {
				Default(false)
			})
			Required("created_by", "request")
		})
		Result(DevBuild)
		HTTP(func() {
			POST("/")
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
			GET("/{id}")
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
			PUT("/{id}")
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
			POST("/{id}/rerun")
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

var DevBuildRequest = Type("DevBuildRequest", func() {
	Attribute("build_env", String)
	Attribute("builder_img", String)
	Attribute("edition", ProductEdition)
	Attribute("features", String)
	Attribute("gitRef", String)
	Attribute("githubRepo", String)
	Attribute("is_hotfix", Boolean)
	Attribute("is_push_gcr", Boolean)
	Attribute("pipeline_engine", PipelineEngine)
	Attribute("plugin_git_ref", String)
	Attribute("product", Product)
	Attribute("productBaseImg", String)
	Attribute("productDockerfile", String)
	Attribute("targetImg", String)
	Attribute("version", String)

	Required("edition", "gitRef", "product", "version")
})

var DevBuild = Type("DevBuild", func() {
	Attribute("id", Int)
	Attribute("meta", DevBuildMeta)
	Attribute("spec", DevBuildSpec)
	Attribute("status", DevBuildStatus)
	Required("id", "meta", "spec", "status")
})

var DevBuildMeta = Type("DevBuildMeta", func() {
	Attribute("created_by", String, func() {
		Format(FormatEmail)
	})
	Attribute("created_at", String, func() {
		Format(FormatDateTime)
	})
	Attribute("updated_at", String, func() {
		Format(FormatDateTime)
	})
	Required("created_at", "created_by", "updated_at")
})

var DevBuildSpec = Type("DevBuildSpec", func() {
	Attribute("build_env", String)
	Attribute("builder_img", String)
	Attribute("edition", ProductEdition)
	Attribute("features", String)
	Attribute("gitHash", String)
	Attribute("gitRef", String)
	Attribute("githubRepo", String)
	Attribute("is_hotfix", Boolean)
	Attribute("is_push_gcr", Boolean)
	Attribute("pipeline_engine", PipelineEngine)
	Attribute("plugin_git_ref", String)
	Attribute("product", Product)
	Attribute("productBaseImg", String)
	Attribute("productDockerfile", String)
	Attribute("targetImg", String)
	Attribute("version", String)
	Required("build_env", "builder_img", "edition", "features", "gitHash", "gitRef", "githubRepo", "is_hotfix", "is_push_gcr", "pipeline_engine", "plugin_git_ref", "product", "productBaseImg", "productDockerfile", "targetImg", "version")
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
