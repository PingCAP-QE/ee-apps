// Code generated by goa v3.20.0, DO NOT EDIT.
//
// devbuild HTTP client CLI support package
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/tibuild/internal/service/design -o
// ./service

package client

import (
	"encoding/json"
	"fmt"
	"strconv"

	devbuild "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
	goa "goa.design/goa/v3/pkg"
)

// BuildListPayload builds the payload for the devbuild list endpoint from CLI
// flags.
func BuildListPayload(devbuildListPage string, devbuildListPageSize string, devbuildListHotfix string, devbuildListSort string, devbuildListDirection string, devbuildListCreatedBy string) (*devbuild.ListPayload, error) {
	var err error
	var page int
	{
		if devbuildListPage != "" {
			var v int64
			v, err = strconv.ParseInt(devbuildListPage, 10, strconv.IntSize)
			page = int(v)
			if err != nil {
				return nil, fmt.Errorf("invalid value for page, must be INT")
			}
		}
	}
	var pageSize int
	{
		if devbuildListPageSize != "" {
			var v int64
			v, err = strconv.ParseInt(devbuildListPageSize, 10, strconv.IntSize)
			pageSize = int(v)
			if err != nil {
				return nil, fmt.Errorf("invalid value for pageSize, must be INT")
			}
		}
	}
	var hotfix bool
	{
		if devbuildListHotfix != "" {
			hotfix, err = strconv.ParseBool(devbuildListHotfix)
			if err != nil {
				return nil, fmt.Errorf("invalid value for hotfix, must be BOOL")
			}
		}
	}
	var sort string
	{
		if devbuildListSort != "" {
			sort = devbuildListSort
			if !(sort == "created_at" || sort == "updated_at") {
				err = goa.MergeErrors(err, goa.InvalidEnumValueError("sort", sort, []any{"created_at", "updated_at"}))
			}
			if err != nil {
				return nil, err
			}
		}
	}
	var direction string
	{
		if devbuildListDirection != "" {
			direction = devbuildListDirection
			if !(direction == "asc" || direction == "desc") {
				err = goa.MergeErrors(err, goa.InvalidEnumValueError("direction", direction, []any{"asc", "desc"}))
			}
			if err != nil {
				return nil, err
			}
		}
	}
	var createdBy *string
	{
		if devbuildListCreatedBy != "" {
			createdBy = &devbuildListCreatedBy
		}
	}
	v := &devbuild.ListPayload{}
	v.Page = page
	v.PageSize = pageSize
	v.Hotfix = hotfix
	v.Sort = sort
	v.Direction = direction
	v.CreatedBy = createdBy

	return v, nil
}

// BuildCreatePayload builds the payload for the devbuild create endpoint from
// CLI flags.
func BuildCreatePayload(devbuildCreateBody string, devbuildCreateDryrun string) (*devbuild.CreatePayload, error) {
	var err error
	var body CreateRequestBody
	{
		err = json.Unmarshal([]byte(devbuildCreateBody), &body)
		if err != nil {
			return nil, fmt.Errorf("invalid JSON for body, \nerror: %s, \nexample of valid JSON:\n%s", err, "'{\n      \"created_by\": \"audrey.paucek@ratke.info\",\n      \"request\": {\n         \"build_env\": \"Ipsa et alias.\",\n         \"builder_img\": \"Et ipsa.\",\n         \"edition\": \"enterprise\",\n         \"features\": \"Veniam eaque nisi.\",\n         \"git_ref\": \"Aut adipisci sed.\",\n         \"git_sha\": \"Eum sit.\",\n         \"github_repo\": \"Suscipit et.\",\n         \"is_hotfix\": true,\n         \"is_push_gcr\": true,\n         \"pipeline_engine\": \"tekton\",\n         \"plugin_git_ref\": \"Eum vel officiis quasi sit a ex.\",\n         \"product\": \"pd\",\n         \"product_base_img\": \"Nemo harum.\",\n         \"product_dockerfile\": \"Reprehenderit eaque exercitationem.\",\n         \"target_img\": \"Dolorem blanditiis velit voluptatem exercitationem et.\",\n         \"version\": \"Cumque magnam error officiis impedit quaerat consectetur.\"\n      }\n   }'")
		}
		if body.Request == nil {
			err = goa.MergeErrors(err, goa.MissingFieldError("request", "body"))
		}
		err = goa.MergeErrors(err, goa.ValidateFormat("body.created_by", body.CreatedBy, goa.FormatEmail))
		if body.Request != nil {
			if err2 := ValidateDevBuildSpecRequestBody(body.Request); err2 != nil {
				err = goa.MergeErrors(err, err2)
			}
		}
		if err != nil {
			return nil, err
		}
	}
	var dryrun bool
	{
		if devbuildCreateDryrun != "" {
			dryrun, err = strconv.ParseBool(devbuildCreateDryrun)
			if err != nil {
				return nil, fmt.Errorf("invalid value for dryrun, must be BOOL")
			}
		}
	}
	v := &devbuild.CreatePayload{
		CreatedBy: body.CreatedBy,
	}
	if body.Request != nil {
		v.Request = marshalDevBuildSpecRequestBodyToDevbuildDevBuildSpec(body.Request)
	}
	v.Dryrun = dryrun

	return v, nil
}

// BuildGetPayload builds the payload for the devbuild get endpoint from CLI
// flags.
func BuildGetPayload(devbuildGetID string, devbuildGetSync string) (*devbuild.GetPayload, error) {
	var err error
	var id int
	{
		var v int64
		v, err = strconv.ParseInt(devbuildGetID, 10, strconv.IntSize)
		id = int(v)
		if err != nil {
			return nil, fmt.Errorf("invalid value for id, must be INT")
		}
	}
	var sync bool
	{
		if devbuildGetSync != "" {
			sync, err = strconv.ParseBool(devbuildGetSync)
			if err != nil {
				return nil, fmt.Errorf("invalid value for sync, must be BOOL")
			}
		}
	}
	v := &devbuild.GetPayload{}
	v.ID = id
	v.Sync = sync

	return v, nil
}

// BuildUpdatePayload builds the payload for the devbuild update endpoint from
// CLI flags.
func BuildUpdatePayload(devbuildUpdateBody string, devbuildUpdateID string, devbuildUpdateDryrun string) (*devbuild.UpdatePayload, error) {
	var err error
	var body UpdateRequestBody
	{
		err = json.Unmarshal([]byte(devbuildUpdateBody), &body)
		if err != nil {
			return nil, fmt.Errorf("invalid JSON for body, \nerror: %s, \nexample of valid JSON:\n%s", err, "'{\n      \"status\": {\n         \"build_report\": {\n            \"binaries\": [\n               {\n                  \"component\": \"Dicta officiis magni enim qui.\",\n                  \"oci_file\": {\n                     \"file\": \"Doloribus dolor officiis nihil rerum.\",\n                     \"repo\": \"Qui veniam voluptates nisi ex repellat quae.\",\n                     \"tag\": \"Eos et ab et sed pariatur.\"\n                  },\n                  \"platform\": \"Quo nulla.\",\n                  \"sha256_oci_file\": {\n                     \"file\": \"Doloribus dolor officiis nihil rerum.\",\n                     \"repo\": \"Qui veniam voluptates nisi ex repellat quae.\",\n                     \"tag\": \"Eos et ab et sed pariatur.\"\n                  },\n                  \"sha256_url\": \"http://legros.com/bernita\",\n                  \"url\": \"http://cassin.info/bertram_cummings\"\n               },\n               {\n                  \"component\": \"Dicta officiis magni enim qui.\",\n                  \"oci_file\": {\n                     \"file\": \"Doloribus dolor officiis nihil rerum.\",\n                     \"repo\": \"Qui veniam voluptates nisi ex repellat quae.\",\n                     \"tag\": \"Eos et ab et sed pariatur.\"\n                  },\n                  \"platform\": \"Quo nulla.\",\n                  \"sha256_oci_file\": {\n                     \"file\": \"Doloribus dolor officiis nihil rerum.\",\n                     \"repo\": \"Qui veniam voluptates nisi ex repellat quae.\",\n                     \"tag\": \"Eos et ab et sed pariatur.\"\n                  },\n                  \"sha256_url\": \"http://legros.com/bernita\",\n                  \"url\": \"http://cassin.info/bertram_cummings\"\n               }\n            ],\n            \"git_sha\": \"xn9\",\n            \"images\": [\n               {\n                  \"platform\": \"Facilis ut libero doloribus beatae.\",\n                  \"url\": \"http://simoniscruickshank.org/myrtie\"\n               },\n               {\n                  \"platform\": \"Facilis ut libero doloribus beatae.\",\n                  \"url\": \"http://simoniscruickshank.org/myrtie\"\n               },\n               {\n                  \"platform\": \"Facilis ut libero doloribus beatae.\",\n                  \"url\": \"http://simoniscruickshank.org/myrtie\"\n               },\n               {\n                  \"platform\": \"Facilis ut libero doloribus beatae.\",\n                  \"url\": \"http://simoniscruickshank.org/myrtie\"\n               }\n            ],\n            \"plugin_git_sha\": \"efi\",\n            \"printed_version\": \"Dolores sequi minima eos sed.\"\n         },\n         \"err_msg\": \"Tempore consectetur quos odio.\",\n         \"pipeline_build_id\": 1290727841481834534,\n         \"pipeline_end_at\": \"5101-41-98 76:32:32\",\n         \"pipeline_start_at\": \"1610-85-81 04:74:79\",\n         \"pipeline_view_url\": \"http://carterthompson.org/german.blanda\",\n         \"pipeline_view_urls\": [\n            \"http://lueilwitz.info/floyd.robel\",\n            \"http://ebert.com/bernita_keebler\"\n         ],\n         \"status\": \"processing\",\n         \"tekton_status\": {\n            \"pipelines\": [\n               {\n                  \"end_at\": \"2000-06-19T13:02:59Z\",\n                  \"git_sha\": \"wa6\",\n                  \"images\": [\n                     {\n                        \"platform\": \"Facilis ut libero doloribus beatae.\",\n                        \"url\": \"http://simoniscruickshank.org/myrtie\"\n                     },\n                     {\n                        \"platform\": \"Facilis ut libero doloribus beatae.\",\n                        \"url\": \"http://simoniscruickshank.org/myrtie\"\n                     }\n                  ],\n                  \"name\": \"Ut quibusdam.\",\n                  \"oci_artifacts\": [\n                     {\n                        \"files\": [\n                           \"Incidunt rerum.\",\n                           \"Non quam.\"\n                        ],\n                        \"repo\": \"Tenetur facere quia aspernatur voluptatem.\",\n                        \"tag\": \"Sit ab est laboriosam.\"\n                     },\n                     {\n                        \"files\": [\n                           \"Incidunt rerum.\",\n                           \"Non quam.\"\n                        ],\n                        \"repo\": \"Tenetur facere quia aspernatur voluptatem.\",\n                        \"tag\": \"Sit ab est laboriosam.\"\n                     },\n                     {\n                        \"files\": [\n                           \"Incidunt rerum.\",\n                           \"Non quam.\"\n                        ],\n                        \"repo\": \"Tenetur facere quia aspernatur voluptatem.\",\n                        \"tag\": \"Sit ab est laboriosam.\"\n                     }\n                  ],\n                  \"platform\": \"Aut nemo blanditiis.\",\n                  \"start_at\": \"2013-03-07T11:30:03Z\",\n                  \"status\": \"aborted\",\n                  \"url\": \"http://paucek.net/annamarie.senger\"\n               },\n               {\n                  \"end_at\": \"2000-06-19T13:02:59Z\",\n                  \"git_sha\": \"wa6\",\n                  \"images\": [\n                     {\n                        \"platform\": \"Facilis ut libero doloribus beatae.\",\n                        \"url\": \"http://simoniscruickshank.org/myrtie\"\n                     },\n                     {\n                        \"platform\": \"Facilis ut libero doloribus beatae.\",\n                        \"url\": \"http://simoniscruickshank.org/myrtie\"\n                     }\n                  ],\n                  \"name\": \"Ut quibusdam.\",\n                  \"oci_artifacts\": [\n                     {\n                        \"files\": [\n                           \"Incidunt rerum.\",\n                           \"Non quam.\"\n                        ],\n                        \"repo\": \"Tenetur facere quia aspernatur voluptatem.\",\n                        \"tag\": \"Sit ab est laboriosam.\"\n                     },\n                     {\n                        \"files\": [\n                           \"Incidunt rerum.\",\n                           \"Non quam.\"\n                        ],\n                        \"repo\": \"Tenetur facere quia aspernatur voluptatem.\",\n                        \"tag\": \"Sit ab est laboriosam.\"\n                     },\n                     {\n                        \"files\": [\n                           \"Incidunt rerum.\",\n                           \"Non quam.\"\n                        ],\n                        \"repo\": \"Tenetur facere quia aspernatur voluptatem.\",\n                        \"tag\": \"Sit ab est laboriosam.\"\n                     }\n                  ],\n                  \"platform\": \"Aut nemo blanditiis.\",\n                  \"start_at\": \"2013-03-07T11:30:03Z\",\n                  \"status\": \"aborted\",\n                  \"url\": \"http://paucek.net/annamarie.senger\"\n               }\n            ]\n         }\n      }\n   }'")
		}
		if body.Status == nil {
			err = goa.MergeErrors(err, goa.MissingFieldError("status", "body"))
		}
		if body.Status != nil {
			if err2 := ValidateDevBuildStatusRequestBody(body.Status); err2 != nil {
				err = goa.MergeErrors(err, err2)
			}
		}
		if err != nil {
			return nil, err
		}
	}
	var id int
	{
		var v int64
		v, err = strconv.ParseInt(devbuildUpdateID, 10, strconv.IntSize)
		id = int(v)
		if err != nil {
			return nil, fmt.Errorf("invalid value for id, must be INT")
		}
	}
	var dryrun bool
	{
		if devbuildUpdateDryrun != "" {
			dryrun, err = strconv.ParseBool(devbuildUpdateDryrun)
			if err != nil {
				return nil, fmt.Errorf("invalid value for dryrun, must be BOOL")
			}
		}
	}
	v := &devbuild.UpdatePayload{}
	if body.Status != nil {
		v.Status = marshalDevBuildStatusRequestBodyToDevbuildDevBuildStatus(body.Status)
	}
	v.ID = id
	v.Dryrun = dryrun

	return v, nil
}

// BuildRerunPayload builds the payload for the devbuild rerun endpoint from
// CLI flags.
func BuildRerunPayload(devbuildRerunID string, devbuildRerunDryrun string) (*devbuild.RerunPayload, error) {
	var err error
	var id int
	{
		var v int64
		v, err = strconv.ParseInt(devbuildRerunID, 10, strconv.IntSize)
		id = int(v)
		if err != nil {
			return nil, fmt.Errorf("invalid value for id, must be INT")
		}
	}
	var dryrun bool
	{
		if devbuildRerunDryrun != "" {
			dryrun, err = strconv.ParseBool(devbuildRerunDryrun)
			if err != nil {
				return nil, fmt.Errorf("invalid value for dryrun, must be BOOL")
			}
		}
	}
	v := &devbuild.RerunPayload{}
	v.ID = id
	v.Dryrun = dryrun

	return v, nil
}

// BuildIngestEventPayload builds the payload for the devbuild ingestEvent
// endpoint from CLI flags.
func BuildIngestEventPayload(devbuildIngestEventBody string, devbuildIngestEventDatacontenttype string, devbuildIngestEventID string, devbuildIngestEventSource string, devbuildIngestEventType string, devbuildIngestEventSpecversion string, devbuildIngestEventTime string) (*devbuild.CloudEventIngestEventPayload, error) {
	var err error
	var body IngestEventRequestBody
	{
		err = json.Unmarshal([]byte(devbuildIngestEventBody), &body)
		if err != nil {
			return nil, fmt.Errorf("invalid JSON for body, \nerror: %s, \nexample of valid JSON:\n%s", err, "'{\n      \"data\": {\n         \"buildId\": \"123\",\n         \"duration\": 3600,\n         \"status\": \"success\",\n         \"version\": \"v6.1.0\"\n      },\n      \"dataschema\": \"https://example.com/registry/schemas/build-event.json\",\n      \"subject\": \"tidb-build-123\"\n   }'")
		}
		if body.Data == nil {
			err = goa.MergeErrors(err, goa.MissingFieldError("data", "body"))
		}
		if err != nil {
			return nil, err
		}
	}
	var datacontenttype *string
	{
		if devbuildIngestEventDatacontenttype != "" {
			datacontenttype = &devbuildIngestEventDatacontenttype
		}
	}
	var id string
	{
		id = devbuildIngestEventID
	}
	var source string
	{
		source = devbuildIngestEventSource
	}
	var type_ string
	{
		type_ = devbuildIngestEventType
	}
	var specversion string
	{
		specversion = devbuildIngestEventSpecversion
	}
	var time_ string
	{
		time_ = devbuildIngestEventTime
		err = goa.MergeErrors(err, goa.ValidateFormat("time", time_, goa.FormatDateTime))
		if err != nil {
			return nil, err
		}
	}
	v := &devbuild.CloudEventIngestEventPayload{
		Dataschema: body.Dataschema,
		Subject:    body.Subject,
		Data:       body.Data,
	}
	v.Datacontenttype = datacontenttype
	v.ID = id
	v.Source = source
	v.Type = type_
	v.Specversion = specversion
	v.Time = time_

	return v, nil
}
