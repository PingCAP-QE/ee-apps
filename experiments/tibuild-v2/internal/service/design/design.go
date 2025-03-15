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
			Attribute("build", DevBuild, "Build to update")
			Attribute("dryrun", Boolean, "Dry run", func() {
				Default(false)
			})
			Required("id", "build")
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

var DevBuildRequest = Type("DevBuildRequest", DevBuildSpec, func() {
	Required("product", "version", "edition", "git_ref")
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
	Attribute("edition", String, func() {
		Enum("enterprise", "community")
	})
	Attribute("features", String)
	Attribute("git_ref", String)
	Attribute("git_sha", String)
	Attribute("github_repo", String)
	Attribute("is_hotfix", Boolean)
	Attribute("is_push_gcr", Boolean)
	Attribute("pipeline_engine", String, func() {
		Enum("jenkins", "tekton")
	})
	Attribute("plugin_git_ref", String)
	Attribute("product", String, func() {
		Enum(
			"tidb", "br", "dumpling", "tidb-lightning", // from pingcap/tidb repo.
			"tikv",              // from tikv/tikv repo.
			"pd",                // from tikv/pd repo.
			"enterprise-plugin", // from pingcap-inc/enterprise-plugin repo.
			"tiflash",           // from pingcap/tiflash repo.
			"ticdc", "dm",       // from pingcap/tiflow repo.
			"tidb-binlog", "drainer", "pump", // from pingcap/tidb-binlog repo.
			"tidb-tools",     // from pingcap/tidb-tools repo.
			"ng-monitoring",  // from pingcap/ng-monitoring repo.
			"tidb-dashboard", // from pingcap/tidb-dashboard repo.
			"ticdc-newarch",  // from pingcap/ticdc repo.
		)
	})
	Attribute("product_base_img", String)
	Attribute("product_dockerfile", String)
	Attribute("target_img", String)
	Attribute("version", String)
})

var DevBuildStatus = Type("DevBuildStatus", func() {
	Attribute("build_report", BuildReport)
	Attribute("err_msg", String)
	Attribute("pipeline_build_id", Int)
	Attribute("pipeline_start_at", String, func() { Format(FormatDateTime) })
	Attribute("pipeline_end_at", String, func() { Format(FormatDateTime) })
	Attribute("pipeline_view_url", String, func() { Format(FormatURI) })
	Attribute("pipeline_view_urls", ArrayOf(String, func() { Format(FormatURI) }))
	Attribute("status", BuildStatus)
	Attribute("tekton_status", TektonStatus)

	Required("status")
})

var BuildReport = Type("BuildReport", func() {
	Attribute("binaries", ArrayOf(BinArtifact))
	Attribute("git_sha", String, func() { MaxLength(40) })
	Attribute("images", ArrayOf(ImageArtifact))
	Attribute("plugin_git_sha", String, func() { MaxLength(40) })
	Attribute("printed_version", String)
})

var BinArtifact = Type("BinArtifact", func() {
	Attribute("component", String)
	Attribute("oci_file", OciFile)
	Attribute("platform", String)
	Attribute("sha256_oci_file", OciFile)
	Attribute("sha256_url", String, func() { Format(FormatURI) })
	Attribute("url", String, func() { Format(FormatURI) })
})

var OciFile = Type("OciFile", func() {
	Attribute("file", String)
	Attribute("repo", String)
	Attribute("tag", String)
	Required("file", "repo", "tag")
})

var ImageArtifact = Type("ImageArtifact", func() {
	Attribute("platform", String)
	Attribute("url", String, func() { Format(FormatURI) })
	Required("platform", "url")
})

var TektonStatus = Type("TektonStatus", func() {
	Attribute("pipelines", ArrayOf(TektonPipeline))
	Required("pipelines")
})

var TektonPipeline = Type("TektonPipeline", func() {
	Attribute("name", String)
	Attribute("status", BuildStatus)
	Attribute("start_at", String, func() { Format(FormatDateTime) })
	Attribute("end_at", String, func() { Format(FormatDateTime) })
	Attribute("git_sha", String, func() { MaxLength(40) })
	Attribute("images", ArrayOf(ImageArtifact))
	Attribute("oci_artifacts", ArrayOf(OciArtifact))
	Attribute("platform", String)
	Attribute("url", String, func() { Format(FormatURI) })

	Required("name", "status")
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

var HTTPError = Type("HTTPError", func() {
	Attribute("code", Int)
	Attribute("message", String)
	Required("code", "message")
})
