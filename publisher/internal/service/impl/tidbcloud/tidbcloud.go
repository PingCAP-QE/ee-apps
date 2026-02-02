package tidbcloud

import (
	"context"

	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/gen/tidbcloud"
	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
)

// tidbcloud service example implementation.
// The example methods log the requests and return zero values.
type tidbcloudsrvc struct {
	*share.BaseService
}

// NewService returns the tidbcloud service implementation.
func NewService(logger *zerolog.Logger, cfg config.Service) tidbcloud.Service {
	return &tidbcloudsrvc{
		BaseService: share.NewBaseServiceService(logger, cfg),
	}
}

func (s *tidbcloudsrvc) callOpsPlatformAPI() {

}

// UpdateComponentVersionInCloudconfig implements
// update-component-version-in-cloudconfig.
func (s *tidbcloudsrvc) UpdateComponentVersionInCloudconfig(ctx context.Context, p *tidbcloud.UpdateComponentVersionInCloudconfigPayload) (res *tidbcloud.UpdateComponentVersionInCloudconfigResult, err error) {
	res = &tidbcloud.UpdateComponentVersionInCloudconfigResult{}
	s.Logger.Info().Msgf("tidbcloud.update-component-version-in-cloudconfig")

	// function make_ops_api_call() {
	//            local stage="$1"
	//            local component="$2"
	//            local component_version="$3"
	//            local cluster_type="$4"
	//            local image_repo="$5"
	//            local image_tag="$6"
	//            local github_repo="$7"
	//            local api_base_url="$8"
	//            local api_key="$9"
	//            local save_file="$10"

	//            local api_url="$api_base_url/$component"

	//            # get the tag metadata from github
	//            local tag_metadata=$(get_git_tag_metadata "$github_repo" "$image_tag")

	//            # prepare payload file
	//            local payload_file="/tmp/$component-$component_version.json"
	//            jq -n \
	//              --arg cluster_type "$cluster_type" \
	//              --arg version "$component_version" \
	//              --arg base_image "$image_repo" \
	//              --arg tag "$image_tag" \

	//            echo "🚀 Request to update the image for component $component@$component_version in stage $stage with: $image_repo:$image_tag"
	//            local response_file="/tmp/${component}-${component_version}-response.json"
	//            curl -f \
	//              --request POST \
	//              --location "$api_url" \
	//              --header "x-api-key: $api_key" \
	//              --header "Content-Type: application/json" \
	//              --data "@$payload_file" \
	//              --output "$response_file"
	//            ops_instance_id=$(jq -r .instance_id $response_file)
	//            ops_ticket_url="$(get_ops_instance_url $stage $ops_instance_id)"
	//            echo "✅ Requested image updating successfully for component $component@$component_version in stage $stage with: $image_repo:$image_tag, the Ops ticket URL is: $ops_ticket_url"
	//            echo "$component@$component_version in $stage stage: $ops_ticket_url" >> "$save_file"
	//        }

	//        function update_ops_config_for_image() {
	//          local stage="$1"
	//          local image="$2"
	//          local stage_config_file="$3"
	//          local save_file="$4"
	//          local api_base_url="$(jq -r '.api_base_url' $stage_config_file)"
	//          local api_key="$(jq -r '.api_key' $stage_config_file)"

	//          # split image name and tag
	//          local image_repo=${image%:*}
	//          local image_tag=${image##*:}

	//          # get the component name from image repo: the base name of the image repo
	//          local components=$(jq -r --arg img "$image_repo" '.components | to_entries | map(select(.value.base_image==$img) | .key) | join(",")' $stage_config_file)
	//          if [ -z "$components" ]; then
	//            echo "🤷 No component matched to bump on the Ops platform, skip."
	//            return 0
	//          fi

	//          # get the component version from image tag(should return the vX.Y.Z part)
	//          local component_version=$(echo $image_tag | cut -d'-' -f1)

	//          # loop the components
	//          IFS=',' read -ra components_array <<< "$components"
	//          for component in "${components_array[@]}"; do
	//            cluster_type="$(jq -r ".components.${component}.cluster_type" $stage_config_file)"
	//            github_repo="$(jq -r ".components.${component}.github_repo" $stage_config_file)"
	//            make_ops_api_call "$stage" "$component" "$component_version" "$cluster_type" "$image_repo" "$image_tag" "$github_repo" "$api_base_url" "$api_key" "$save_file"
	//          done
	//        }

	return
}
