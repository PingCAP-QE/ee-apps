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
			return nil, fmt.Errorf("invalid JSON for body, \nerror: %s, \nexample of valid JSON:\n%s", err, "'{\n      \"created_by\": \"alford_lowe@dubuque.net\",\n      \"request\": {\n         \"build_env\": \"Adipisci nostrum blanditiis architecto libero est delectus.\",\n         \"builder_img\": \"Harum magni est ipsa et.\",\n         \"edition\": \"community\",\n         \"features\": \"Et ipsa.\",\n         \"git_ref\": \"Sed veniam eaque.\",\n         \"git_sha\": \"Facilis aut adipisci.\",\n         \"github_repo\": \"Velit eum sit.\",\n         \"is_hotfix\": true,\n         \"is_push_gcr\": true,\n         \"pipeline_engine\": \"tekton\",\n         \"plugin_git_ref\": \"Ut magni suscipit eum vel officiis quasi.\",\n         \"product\": \"tiflash\",\n         \"product_base_img\": \"Ex amet est nemo harum voluptas.\",\n         \"product_dockerfile\": \"Eaque exercitationem et.\",\n         \"target_img\": \"Blanditiis velit voluptatem exercitationem.\",\n         \"version\": \"Est cumque magnam error.\"\n      }\n   }'")
		}
		if body.Request == nil {
			err = goa.MergeErrors(err, goa.MissingFieldError("request", "body"))
		}
		err = goa.MergeErrors(err, goa.ValidateFormat("body.created_by", body.CreatedBy, goa.FormatEmail))
		if body.Request != nil {
			if err2 := ValidateDevBuildRequestRequestBody(body.Request); err2 != nil {
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
		v.Request = marshalDevBuildRequestRequestBodyToDevbuildDevBuildRequest(body.Request)
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
			return nil, fmt.Errorf("invalid JSON for body, \nerror: %s, \nexample of valid JSON:\n%s", err, "'{\n      \"build\": {\n         \"id\": 6768229537905997887,\n         \"meta\": {\n            \"created_at\": \"2769-76-81 76:71:23\",\n            \"created_by\": \"hilbert.kunze@hessel.biz\",\n            \"updated_at\": \"2588-93-63 54:89:92\"\n         },\n         \"spec\": {\n            \"build_env\": \"Adipisci nostrum blanditiis architecto libero est delectus.\",\n            \"builder_img\": \"Harum magni est ipsa et.\",\n            \"edition\": \"community\",\n            \"features\": \"Et ipsa.\",\n            \"git_ref\": \"Sed veniam eaque.\",\n            \"git_sha\": \"Facilis aut adipisci.\",\n            \"github_repo\": \"Velit eum sit.\",\n            \"is_hotfix\": true,\n            \"is_push_gcr\": true,\n            \"pipeline_engine\": \"tekton\",\n            \"plugin_git_ref\": \"Ut magni suscipit eum vel officiis quasi.\",\n            \"product\": \"tiflash\",\n            \"product_base_img\": \"Ex amet est nemo harum voluptas.\",\n            \"product_dockerfile\": \"Eaque exercitationem et.\",\n            \"target_img\": \"Blanditiis velit voluptatem exercitationem.\",\n            \"version\": \"Est cumque magnam error.\"\n         },\n         \"status\": {\n            \"build_report\": {\n               \"binaries\": [\n                  {\n                     \"component\": \"Commodi et aut.\",\n                     \"oci_file\": {\n                        \"file\": \"Assumenda quia.\",\n                        \"repo\": \"Saepe quas in animi.\",\n                        \"tag\": \"Mollitia qui ex fuga sit harum.\"\n                     },\n                     \"platform\": \"Laboriosam magnam rerum.\",\n                     \"sha256_oci_file\": {\n                        \"file\": \"Assumenda quia.\",\n                        \"repo\": \"Saepe quas in animi.\",\n                        \"tag\": \"Mollitia qui ex fuga sit harum.\"\n                     },\n                     \"sha256_url\": \"http://beier.com/marielle\",\n                     \"url\": \"http://shields.org/madie\"\n                  },\n                  {\n                     \"component\": \"Commodi et aut.\",\n                     \"oci_file\": {\n                        \"file\": \"Assumenda quia.\",\n                        \"repo\": \"Saepe quas in animi.\",\n                        \"tag\": \"Mollitia qui ex fuga sit harum.\"\n                     },\n                     \"platform\": \"Laboriosam magnam rerum.\",\n                     \"sha256_oci_file\": {\n                        \"file\": \"Assumenda quia.\",\n                        \"repo\": \"Saepe quas in animi.\",\n                        \"tag\": \"Mollitia qui ex fuga sit harum.\"\n                     },\n                     \"sha256_url\": \"http://beier.com/marielle\",\n                     \"url\": \"http://shields.org/madie\"\n                  },\n                  {\n                     \"component\": \"Commodi et aut.\",\n                     \"oci_file\": {\n                        \"file\": \"Assumenda quia.\",\n                        \"repo\": \"Saepe quas in animi.\",\n                        \"tag\": \"Mollitia qui ex fuga sit harum.\"\n                     },\n                     \"platform\": \"Laboriosam magnam rerum.\",\n                     \"sha256_oci_file\": {\n                        \"file\": \"Assumenda quia.\",\n                        \"repo\": \"Saepe quas in animi.\",\n                        \"tag\": \"Mollitia qui ex fuga sit harum.\"\n                     },\n                     \"sha256_url\": \"http://beier.com/marielle\",\n                     \"url\": \"http://shields.org/madie\"\n                  },\n                  {\n                     \"component\": \"Commodi et aut.\",\n                     \"oci_file\": {\n                        \"file\": \"Assumenda quia.\",\n                        \"repo\": \"Saepe quas in animi.\",\n                        \"tag\": \"Mollitia qui ex fuga sit harum.\"\n                     },\n                     \"platform\": \"Laboriosam magnam rerum.\",\n                     \"sha256_oci_file\": {\n                        \"file\": \"Assumenda quia.\",\n                        \"repo\": \"Saepe quas in animi.\",\n                        \"tag\": \"Mollitia qui ex fuga sit harum.\"\n                     },\n                     \"sha256_url\": \"http://beier.com/marielle\",\n                     \"url\": \"http://shields.org/madie\"\n                  }\n               ],\n               \"git_sha\": \"8ef\",\n               \"images\": [\n                  {\n                     \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                     \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                  },\n                  {\n                     \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                     \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                  },\n                  {\n                     \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                     \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                  },\n                  {\n                     \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                     \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                  }\n               ],\n               \"plugin_git_sha\": \"xc0\",\n               \"printed_version\": \"Debitis non ut et asperiores non accusantium.\"\n            },\n            \"err_msg\": \"Labore quod nihil sint unde sint.\",\n            \"pipeline_build_id\": 5577746371005261499,\n            \"pipeline_end_at\": \"8505-06-94 49:95:70\",\n            \"pipeline_start_at\": \"7373-05-24 20:11:71\",\n            \"pipeline_view_url\": \"http://grimes.biz/ethel.stiedemann\",\n            \"pipeline_view_urls\": [\n               \"http://halvorson.org/melissa_konopelski\",\n               \"http://carroll.org/keanu.wuckert\",\n               \"http://kirlin.org/anastasia_pouros\"\n            ],\n            \"status\": \"error\",\n            \"tekton_status\": {\n               \"pipelines\": [\n                  {\n                     \"end_at\": \"1979-06-21T08:02:19Z\",\n                     \"git_sha\": \"ip6\",\n                     \"images\": [\n                        {\n                           \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                           \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                        },\n                        {\n                           \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                           \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                        },\n                        {\n                           \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                           \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                        }\n                     ],\n                     \"name\": \"Laboriosam neque aut nemo.\",\n                     \"oci_artifacts\": [\n                        {\n                           \"files\": [\n                              \"Animi odit.\",\n                              \"Et vel vero.\",\n                              \"Totam aut.\"\n                           ],\n                           \"repo\": \"Ducimus omnis deserunt.\",\n                           \"tag\": \"Commodi dicta qui explicabo occaecati.\"\n                        },\n                        {\n                           \"files\": [\n                              \"Animi odit.\",\n                              \"Et vel vero.\",\n                              \"Totam aut.\"\n                           ],\n                           \"repo\": \"Ducimus omnis deserunt.\",\n                           \"tag\": \"Commodi dicta qui explicabo occaecati.\"\n                        }\n                     ],\n                     \"platform\": \"Iure voluptatibus et qui dignissimos.\",\n                     \"start_at\": \"1984-05-21T11:11:58Z\",\n                     \"status\": \"error\",\n                     \"url\": \"http://ondricka.name/tate\"\n                  },\n                  {\n                     \"end_at\": \"1979-06-21T08:02:19Z\",\n                     \"git_sha\": \"ip6\",\n                     \"images\": [\n                        {\n                           \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                           \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                        },\n                        {\n                           \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                           \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                        },\n                        {\n                           \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                           \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                        }\n                     ],\n                     \"name\": \"Laboriosam neque aut nemo.\",\n                     \"oci_artifacts\": [\n                        {\n                           \"files\": [\n                              \"Animi odit.\",\n                              \"Et vel vero.\",\n                              \"Totam aut.\"\n                           ],\n                           \"repo\": \"Ducimus omnis deserunt.\",\n                           \"tag\": \"Commodi dicta qui explicabo occaecati.\"\n                        },\n                        {\n                           \"files\": [\n                              \"Animi odit.\",\n                              \"Et vel vero.\",\n                              \"Totam aut.\"\n                           ],\n                           \"repo\": \"Ducimus omnis deserunt.\",\n                           \"tag\": \"Commodi dicta qui explicabo occaecati.\"\n                        }\n                     ],\n                     \"platform\": \"Iure voluptatibus et qui dignissimos.\",\n                     \"start_at\": \"1984-05-21T11:11:58Z\",\n                     \"status\": \"error\",\n                     \"url\": \"http://ondricka.name/tate\"\n                  },\n                  {\n                     \"end_at\": \"1979-06-21T08:02:19Z\",\n                     \"git_sha\": \"ip6\",\n                     \"images\": [\n                        {\n                           \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                           \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                        },\n                        {\n                           \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                           \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                        },\n                        {\n                           \"platform\": \"Culpa voluptatem tempora explicabo.\",\n                           \"url\": \"http://kundebeatty.net/toy.lueilwitz\"\n                        }\n                     ],\n                     \"name\": \"Laboriosam neque aut nemo.\",\n                     \"oci_artifacts\": [\n                        {\n                           \"files\": [\n                              \"Animi odit.\",\n                              \"Et vel vero.\",\n                              \"Totam aut.\"\n                           ],\n                           \"repo\": \"Ducimus omnis deserunt.\",\n                           \"tag\": \"Commodi dicta qui explicabo occaecati.\"\n                        },\n                        {\n                           \"files\": [\n                              \"Animi odit.\",\n                              \"Et vel vero.\",\n                              \"Totam aut.\"\n                           ],\n                           \"repo\": \"Ducimus omnis deserunt.\",\n                           \"tag\": \"Commodi dicta qui explicabo occaecati.\"\n                        }\n                     ],\n                     \"platform\": \"Iure voluptatibus et qui dignissimos.\",\n                     \"start_at\": \"1984-05-21T11:11:58Z\",\n                     \"status\": \"error\",\n                     \"url\": \"http://ondricka.name/tate\"\n                  }\n               ]\n            }\n         }\n      }\n   }'")
		}
		if body.Build == nil {
			err = goa.MergeErrors(err, goa.MissingFieldError("build", "body"))
		}
		if body.Build != nil {
			if err2 := ValidateDevBuildRequestBody(body.Build); err2 != nil {
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
	if body.Build != nil {
		v.Build = marshalDevBuildRequestBodyToDevbuildDevBuild(body.Build)
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
