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
			Param("direction")
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
			Attribute("request", DevBuildSpec, "Build to create, only spec field is required, others are ignored", func() {
				Required("product", "version", "edition", "git_ref")
			})
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
			Attribute("status", DevBuildStatus, "Status to update")
			Attribute("dryrun", Boolean, "Dry run", func() {
				Default(false)
			})
			Required("id", "status")
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

	Method("ingestEvent", func() {
		Description("Ingest a CloudEvent for build events")
		Payload(CloudEvent, func() {
			Required("id", "source", "type", "specversion", "time", "data")
		})
		Result(CloudEventResponse)
		HTTP(func() {
			POST("/events")
			PUT("/events")
			Header("datacontenttype:Content-Type")
			Header("id:ce-id")
			Header("source:ce-source")
			Header("type:ce-type")
			Header("specversion:ce-specversion")
			Header("time:ce-time")
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
	Attribute("created_by", String, func() {
		Format(FormatEmail)
	})
	Attribute("created_at", String, func() {
		Pattern(`^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$`)
	})
	Attribute("updated_at", String, func() {
		Pattern(`^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$`)
	})
	Required("created_at", "created_by", "updated_at")
})

var DevBuildSpec = Type("DevBuildSpec", func() {
	Attribute("build_env", String)
	Attribute("builder_img", String)
	Attribute("edition", String, func() {
		Enum("enterprise", "community", "fips", "failpoint", "experiment", "next-gen")
	})
	Attribute("platform", String, func() {
		Default("all")
		Enum("all", "linux", "darwin", "linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64")
	})
	Attribute("features", String, func() {
		Description("[Deprecated] use build_env for custom features")
	})
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
			"dm",                                       // from pingcap/tiflow repo.
			"enterprise-plugin",                        // from pingcap-inc/enterprise-plugin repo.
			"ng-monitoring",                            // from pingcap/ng-monitoring repo.
			"pd",                                       // from tikv/pd repo.
			"ticdc",                                    // from pingcap/tiflow or pingcap/ticdc repo.
			"ticdc-newarch",                            // from pingcap/ticdc repo.
			"tici",           // from pingcap-inc/tici repo.
			"tidb", "br", "dumpling", "tidb-lightning", // from pingcap/tidb repo.
			"tidb-binlog", "drainer", "pump", // from pingcap/tidb-binlog repo.
			"tidb-dashboard", // from pingcap/tidb-dashboard repo.
			"tidb-operator",  // from pingcap/tidb-operator repo.
			"tidb-tools",     // from pingcap/tidb-tools repo.
			"tiflash",        // from pingcap/tiflash repo.
			"tikv",           // from tikv/tikv repo.
			"tiproxy",        // from pingcap/tiproxy repo.
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
	Attribute("pipeline_start_at", String, func() {
		Pattern(`^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$`)
	})
	Attribute("pipeline_end_at", String, func() {
		Pattern(`^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$`)
	})
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
	Attribute("internal_url", String, func() { Format(FormatURI) })
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
	Enum("pending", "processing", "aborted", "success", "failure", "error")
})

var HTTPError = Type("HTTPError", func() {
	Attribute("code", Int)
	Attribute("message", String)
	Required("code", "message")
})

var CloudEvent = Type("CloudEvent", func() {
	Attribute("id", String, "Unique identifier for the event", func() {
		Example("f81d4fae-7dec-11d0-a765-00a0c91e6bf6")
	})
	Attribute("source", String, "Identifies the context in which an event happened", func() {
		Example("/jenkins/build")
	})
	Attribute("type", String, "Describes the type of event related to the originating occurrence", func() {
		Example("com.pingcap.build.complete")
	})
	Attribute("datacontenttype", String, "Content type of the data value")
	Attribute("specversion", String, "The version of the CloudEvents specification which the event uses", func() {
		Example("1.0")
	})
	Attribute("dataschema", String, "Identifies the schema that data adheres to", func() {
		Example("https://example.com/registry/schemas/build-event.json")
	})
	Attribute("subject", String, "Describes the subject of the event in the context of the event producer", func() {
		Example("tidb-build-123")
	})
	Attribute("time", String, "Timestamp of when the occurrence happened", func() {
		Format(FormatDateTime)
		Example("2022-10-01T12:00:00Z")
	})
	Attribute("data", Any, "Event payload", func() {
		Example(map[string]any{
			"buildId":  "123",
			"status":   "success",
			"version":  "v6.1.0",
			"duration": 3600,
		})
	})
})

var CloudEventResponse = Type("CloudEventResponse", func() {
	Attribute("id", String, "The ID of the processed CloudEvent", func() {
		Example("f81d4fae-7dec-11d0-a765-00a0c91e6bf6")
	})
	Attribute("status", String, "Processing status", func() {
		Enum("accepted", "processing", "error")
		Example("accepted")
	})
	Attribute("message", String, "Additional information about processing result", func() {
		Example("Event successfully queued for processing")
	})

	Required("id", "status")
})
