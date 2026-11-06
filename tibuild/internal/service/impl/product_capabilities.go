package impl

import (
	"fmt"
	"net/http"
	"slices"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

type productBuildCapability struct {
	Label           string
	Platforms       []string
	Editions        []string
	PipelineEngines []string
	SupportsHotfix  bool
	SupportsPlugin  bool
}

// Entries declare constraints for products whose build support is restricted.
// Products without an entry retain their existing request validation.
var productBuildCapabilities = map[string]productBuildCapability{
	"tidb-cse":           {Label: "TiDB Serverless", Platforms: []string{"linux", LinuxAmd64, LinuxArm64}, Editions: []string{"community"}, PipelineEngines: []string{tektonEngine}},
	"pd-cse":             {Label: "PD Serverless", Platforms: []string{"linux", LinuxAmd64, LinuxArm64}, Editions: []string{"community"}, PipelineEngines: []string{tektonEngine}},
	"tidb-operator-cse":  {Label: "TiDB Operator Serverless", Platforms: []string{"linux", LinuxAmd64, LinuxArm64}, Editions: []string{"community"}, PipelineEngines: []string{tektonEngine}},
	"cassandra-on-mysql": {Label: "Cassandra on MySQL", Platforms: []string{"linux", LinuxAmd64, LinuxArm64}, Editions: []string{"community"}, PipelineEngines: []string{tektonEngine}},
	"qdrant-on-tidb":     {Label: "Qdrant on TiDB", Platforms: []string{"linux", LinuxAmd64, LinuxArm64}, Editions: []string{"community"}, PipelineEngines: []string{tektonEngine}},
}

func validateProductRequest(request *devbuild.DevBuildSpec) error {
	capability, ok := productBuildCapabilities[request.Product]
	if !ok {
		return nil
	}
	badRequest := func(message string) error {
		return &devbuild.DevBuildBadRequestError{Code: http.StatusBadRequest, Message: fmt.Sprintf("product %q: %s", request.Product, message)}
	}
	if request.Platform != "" && !slices.Contains(capability.Platforms, request.Platform) {
		return badRequest(fmt.Sprintf("supported platforms are %v", capability.Platforms))
	}
	if engine := derefString(request.PipelineEngine); engine != "" && !slices.Contains(capability.PipelineEngines, engine) {
		return badRequest(fmt.Sprintf("supported engines are %v", capability.PipelineEngines))
	}
	if request.Edition != "" && !slices.Contains(capability.Editions, request.Edition) {
		return badRequest(fmt.Sprintf("supported editions are %v", capability.Editions))
	}
	if request.IsHotfix != nil && *request.IsHotfix && !capability.SupportsHotfix {
		return badRequest("hotfix builds are unsupported")
	}
	if derefString(request.PluginGitRef) != "" && !capability.SupportsPlugin {
		return badRequest("enterprise plugins are unsupported")
	}
	return nil
}
