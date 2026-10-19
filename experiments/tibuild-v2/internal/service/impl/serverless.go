package impl

import (
	"net/http"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

var serverlessProductLabels = map[string]string{
	"tidb-cse":           "TiDB Serverless",
	"pd-cse":             "PD Serverless",
	"tidb-operator-cse":  "TiDB Operator Serverless",
	"cassandra-on-mysql": "Cassandra on MySQL",
	"qdrant-on-tidb":     "Qdrant on TiDB",
}

func isServerlessProduct(product string) bool {
	_, ok := serverlessProductLabels[product]
	return ok
}

func validateServerlessRequest(request *devbuild.DevBuildSpec) error {
	if !isServerlessProduct(request.Product) {
		return nil
	}
	badRequest := func(message string) error {
		return &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: message}
	}
	if request.Platform != "" && request.Platform != "linux" && request.Platform != LinuxAmd64 && request.Platform != LinuxArm64 {
		return badRequest("Serverless devbuild supports linux, linux/amd64 and linux/arm64 only")
	}
	if engine := derefString(request.PipelineEngine); engine != "" && engine != tektonEngine {
		return badRequest("Serverless devbuild requires the tekton engine")
	}
	if request.Edition != "" && request.Edition != "community" {
		return badRequest("Serverless devbuild supports the community (release) profile only")
	}
	if request.IsHotfix != nil && *request.IsHotfix {
		return badRequest("Serverless products support devbuild, not the hotfix workflow")
	}
	if derefString(request.PluginGitRef) != "" {
		return badRequest("Serverless devbuild does not use an external enterprise plugin")
	}
	return nil
}
