// Code generated by goa v3.20.0, DO NOT EDIT.
//
// tibuild HTTP client CLI support package
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/tibuild/internal/service/design -o
// ./service

package cli

import (
	"flag"
	"fmt"
	"net/http"
	"os"

	artifactc "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/http/artifact/client"
	devbuildc "github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/http/devbuild/client"
	goahttp "goa.design/goa/v3/http"
	goa "goa.design/goa/v3/pkg"
)

// UsageCommands returns the set of commands and sub-commands using the format
//
//	command (subcommand1|subcommand2|...)
func UsageCommands() string {
	return `artifact sync-image
devbuild (list|create|get|update|rerun)
`
}

// UsageExamples produces an example of a valid invocation of the CLI tool.
func UsageExamples() string {
	return os.Args[0] + ` artifact sync-image --body '{
      "source": "Ea ducimus eius et.",
      "target": "Praesentium sed illum esse sint."
   }'` + "\n" +
		os.Args[0] + ` devbuild list --page 6473384119965130440 --page-size 550924776103210289 --hotfix true --sort "updated_at" --direction "asc" --created-by "Occaecati facilis consequatur temporibus sit id."` + "\n" +
		""
}

// ParseEndpoint returns the endpoint and payload as specified on the command
// line.
func ParseEndpoint(
	scheme, host string,
	doer goahttp.Doer,
	enc func(*http.Request) goahttp.Encoder,
	dec func(*http.Response) goahttp.Decoder,
	restore bool,
) (goa.Endpoint, any, error) {
	var (
		artifactFlags = flag.NewFlagSet("artifact", flag.ContinueOnError)

		artifactSyncImageFlags    = flag.NewFlagSet("sync-image", flag.ExitOnError)
		artifactSyncImageBodyFlag = artifactSyncImageFlags.String("body", "REQUIRED", "")

		devbuildFlags = flag.NewFlagSet("devbuild", flag.ContinueOnError)

		devbuildListFlags         = flag.NewFlagSet("list", flag.ExitOnError)
		devbuildListPageFlag      = devbuildListFlags.String("page", "1", "")
		devbuildListPageSizeFlag  = devbuildListFlags.String("page-size", "30", "")
		devbuildListHotfixFlag    = devbuildListFlags.String("hotfix", "", "")
		devbuildListSortFlag      = devbuildListFlags.String("sort", "created_at", "")
		devbuildListDirectionFlag = devbuildListFlags.String("direction", "desc", "")
		devbuildListCreatedByFlag = devbuildListFlags.String("created-by", "", "")

		devbuildCreateFlags      = flag.NewFlagSet("create", flag.ExitOnError)
		devbuildCreateBodyFlag   = devbuildCreateFlags.String("body", "REQUIRED", "")
		devbuildCreateDryrunFlag = devbuildCreateFlags.String("dryrun", "", "")

		devbuildGetFlags    = flag.NewFlagSet("get", flag.ExitOnError)
		devbuildGetIDFlag   = devbuildGetFlags.String("id", "REQUIRED", "ID of build")
		devbuildGetSyncFlag = devbuildGetFlags.String("sync", "", "")

		devbuildUpdateFlags      = flag.NewFlagSet("update", flag.ExitOnError)
		devbuildUpdateBodyFlag   = devbuildUpdateFlags.String("body", "REQUIRED", "")
		devbuildUpdateIDFlag     = devbuildUpdateFlags.String("id", "REQUIRED", "ID of build")
		devbuildUpdateDryrunFlag = devbuildUpdateFlags.String("dryrun", "", "")

		devbuildRerunFlags      = flag.NewFlagSet("rerun", flag.ExitOnError)
		devbuildRerunIDFlag     = devbuildRerunFlags.String("id", "REQUIRED", "ID of build")
		devbuildRerunDryrunFlag = devbuildRerunFlags.String("dryrun", "", "")
	)
	artifactFlags.Usage = artifactUsage
	artifactSyncImageFlags.Usage = artifactSyncImageUsage

	devbuildFlags.Usage = devbuildUsage
	devbuildListFlags.Usage = devbuildListUsage
	devbuildCreateFlags.Usage = devbuildCreateUsage
	devbuildGetFlags.Usage = devbuildGetUsage
	devbuildUpdateFlags.Usage = devbuildUpdateUsage
	devbuildRerunFlags.Usage = devbuildRerunUsage

	if err := flag.CommandLine.Parse(os.Args[1:]); err != nil {
		return nil, nil, err
	}

	if flag.NArg() < 2 { // two non flag args are required: SERVICE and ENDPOINT (aka COMMAND)
		return nil, nil, fmt.Errorf("not enough arguments")
	}

	var (
		svcn string
		svcf *flag.FlagSet
	)
	{
		svcn = flag.Arg(0)
		switch svcn {
		case "artifact":
			svcf = artifactFlags
		case "devbuild":
			svcf = devbuildFlags
		default:
			return nil, nil, fmt.Errorf("unknown service %q", svcn)
		}
	}
	if err := svcf.Parse(flag.Args()[1:]); err != nil {
		return nil, nil, err
	}

	var (
		epn string
		epf *flag.FlagSet
	)
	{
		epn = svcf.Arg(0)
		switch svcn {
		case "artifact":
			switch epn {
			case "sync-image":
				epf = artifactSyncImageFlags

			}

		case "devbuild":
			switch epn {
			case "list":
				epf = devbuildListFlags

			case "create":
				epf = devbuildCreateFlags

			case "get":
				epf = devbuildGetFlags

			case "update":
				epf = devbuildUpdateFlags

			case "rerun":
				epf = devbuildRerunFlags

			}

		}
	}
	if epf == nil {
		return nil, nil, fmt.Errorf("unknown %q endpoint %q", svcn, epn)
	}

	// Parse endpoint flags if any
	if svcf.NArg() > 1 {
		if err := epf.Parse(svcf.Args()[1:]); err != nil {
			return nil, nil, err
		}
	}

	var (
		data     any
		endpoint goa.Endpoint
		err      error
	)
	{
		switch svcn {
		case "artifact":
			c := artifactc.NewClient(scheme, host, doer, enc, dec, restore)
			switch epn {
			case "sync-image":
				endpoint = c.SyncImage()
				data, err = artifactc.BuildSyncImagePayload(*artifactSyncImageBodyFlag)
			}
		case "devbuild":
			c := devbuildc.NewClient(scheme, host, doer, enc, dec, restore)
			switch epn {
			case "list":
				endpoint = c.List()
				data, err = devbuildc.BuildListPayload(*devbuildListPageFlag, *devbuildListPageSizeFlag, *devbuildListHotfixFlag, *devbuildListSortFlag, *devbuildListDirectionFlag, *devbuildListCreatedByFlag)
			case "create":
				endpoint = c.Create()
				data, err = devbuildc.BuildCreatePayload(*devbuildCreateBodyFlag, *devbuildCreateDryrunFlag)
			case "get":
				endpoint = c.Get()
				data, err = devbuildc.BuildGetPayload(*devbuildGetIDFlag, *devbuildGetSyncFlag)
			case "update":
				endpoint = c.Update()
				data, err = devbuildc.BuildUpdatePayload(*devbuildUpdateBodyFlag, *devbuildUpdateIDFlag, *devbuildUpdateDryrunFlag)
			case "rerun":
				endpoint = c.Rerun()
				data, err = devbuildc.BuildRerunPayload(*devbuildRerunIDFlag, *devbuildRerunDryrunFlag)
			}
		}
	}
	if err != nil {
		return nil, nil, err
	}

	return endpoint, data, nil
}

// artifactUsage displays the usage of the artifact command and its subcommands.
func artifactUsage() {
	fmt.Fprintf(os.Stderr, `The artifact service provides operations to manage artifacts.
Usage:
    %[1]s [globalflags] artifact COMMAND [flags]

COMMAND:
    sync-image: Sync hotfix image to dockerhub

Additional help:
    %[1]s artifact COMMAND --help
`, os.Args[0])
}
func artifactSyncImageUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] artifact sync-image -body JSON

Sync hotfix image to dockerhub
    -body JSON: 

Example:
    %[1]s artifact sync-image --body '{
      "source": "Ea ducimus eius et.",
      "target": "Praesentium sed illum esse sint."
   }'
`, os.Args[0])
}

// devbuildUsage displays the usage of the devbuild command and its subcommands.
func devbuildUsage() {
	fmt.Fprintf(os.Stderr, `The devbuild service provides operations to manage dev builds.
Usage:
    %[1]s [globalflags] devbuild COMMAND [flags]

COMMAND:
    list: List devbuild with pagination support
    create: Create and trigger devbuild
    get: Get devbuild
    update: Update devbuild status
    rerun: Rerun devbuild

Additional help:
    %[1]s devbuild COMMAND --help
`, os.Args[0])
}
func devbuildListUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] devbuild list -page INT -page-size INT -hotfix BOOL -sort STRING -direction STRING -created-by STRING

List devbuild with pagination support
    -page INT: 
    -page-size INT: 
    -hotfix BOOL: 
    -sort STRING: 
    -direction STRING: 
    -created-by STRING: 

Example:
    %[1]s devbuild list --page 6473384119965130440 --page-size 550924776103210289 --hotfix true --sort "updated_at" --direction "asc" --created-by "Occaecati facilis consequatur temporibus sit id."
`, os.Args[0])
}

func devbuildCreateUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] devbuild create -body JSON -dryrun BOOL

Create and trigger devbuild
    -body JSON: 
    -dryrun BOOL: 

Example:
    %[1]s devbuild create --body '{
      "created_by": "marietta@ernser.com",
      "request": {
         "build_env": "Sunt consequatur veniam rerum quae voluptas consequuntur.",
         "builder_img": "Voluptatibus illo est sed qui dolorem.",
         "edition": "community",
         "features": "Magni dolor.",
         "git_ref": "Eligendi qui culpa libero dignissimos occaecati enim.",
         "git_sha": "Et quaerat odio.",
         "github_repo": "Suscipit nesciunt et porro aliquid.",
         "is_hotfix": true,
         "is_push_gcr": true,
         "pipeline_engine": "jenkins",
         "plugin_git_ref": "Et eum voluptas enim molestiae.",
         "product": "br",
         "product_base_img": "Ut voluptatum quis aut placeat laborum.",
         "product_dockerfile": "Eaque ullam quia vel.",
         "target_img": "Sit consequuntur dolores.",
         "version": "Optio possimus et distinctio nihil cum asperiores."
      }
   }' --dryrun true
`, os.Args[0])
}

func devbuildGetUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] devbuild get -id INT -sync BOOL

Get devbuild
    -id INT: ID of build
    -sync BOOL: 

Example:
    %[1]s devbuild get --id 1 --sync true
`, os.Args[0])
}

func devbuildUpdateUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] devbuild update -body JSON -id INT -dryrun BOOL

Update devbuild status
    -body JSON: 
    -id INT: ID of build
    -dryrun BOOL: 

Example:
    %[1]s devbuild update --body '{
      "build": {
         "id": 6792677507012432024,
         "meta": {
            "created_at": "1996-12-24T11:37:23Z",
            "created_by": "rose.ebert@witting.info",
            "updated_at": "2000-10-19T19:54:20Z"
         },
         "spec": {
            "build_env": "Sunt consequatur veniam rerum quae voluptas consequuntur.",
            "builder_img": "Voluptatibus illo est sed qui dolorem.",
            "edition": "community",
            "features": "Magni dolor.",
            "git_ref": "Eligendi qui culpa libero dignissimos occaecati enim.",
            "git_sha": "Et quaerat odio.",
            "github_repo": "Suscipit nesciunt et porro aliquid.",
            "is_hotfix": true,
            "is_push_gcr": true,
            "pipeline_engine": "jenkins",
            "plugin_git_ref": "Et eum voluptas enim molestiae.",
            "product": "br",
            "product_base_img": "Ut voluptatum quis aut placeat laborum.",
            "product_dockerfile": "Eaque ullam quia vel.",
            "target_img": "Sit consequuntur dolores.",
            "version": "Optio possimus et distinctio nihil cum asperiores."
         },
         "status": {
            "build_report": {
               "binaries": [
                  {
                     "component": "Nulla perferendis.",
                     "oci_file": {
                        "file": "Ullam quod nobis quidem.",
                        "repo": "Delectus et quia dolor perspiciatis animi minus.",
                        "tag": "Nisi ad dignissimos rerum nulla."
                     },
                     "platform": "Labore adipisci aut dolorum omnis voluptatum.",
                     "sha256_oci_file": {
                        "file": "Ullam quod nobis quidem.",
                        "repo": "Delectus et quia dolor perspiciatis animi minus.",
                        "tag": "Nisi ad dignissimos rerum nulla."
                     },
                     "sha256_url": "http://wehnercruickshank.net/claudie",
                     "url": "http://marks.biz/delfina"
                  },
                  {
                     "component": "Nulla perferendis.",
                     "oci_file": {
                        "file": "Ullam quod nobis quidem.",
                        "repo": "Delectus et quia dolor perspiciatis animi minus.",
                        "tag": "Nisi ad dignissimos rerum nulla."
                     },
                     "platform": "Labore adipisci aut dolorum omnis voluptatum.",
                     "sha256_oci_file": {
                        "file": "Ullam quod nobis quidem.",
                        "repo": "Delectus et quia dolor perspiciatis animi minus.",
                        "tag": "Nisi ad dignissimos rerum nulla."
                     },
                     "sha256_url": "http://wehnercruickshank.net/claudie",
                     "url": "http://marks.biz/delfina"
                  },
                  {
                     "component": "Nulla perferendis.",
                     "oci_file": {
                        "file": "Ullam quod nobis quidem.",
                        "repo": "Delectus et quia dolor perspiciatis animi minus.",
                        "tag": "Nisi ad dignissimos rerum nulla."
                     },
                     "platform": "Labore adipisci aut dolorum omnis voluptatum.",
                     "sha256_oci_file": {
                        "file": "Ullam quod nobis quidem.",
                        "repo": "Delectus et quia dolor perspiciatis animi minus.",
                        "tag": "Nisi ad dignissimos rerum nulla."
                     },
                     "sha256_url": "http://wehnercruickshank.net/claudie",
                     "url": "http://marks.biz/delfina"
                  },
                  {
                     "component": "Nulla perferendis.",
                     "oci_file": {
                        "file": "Ullam quod nobis quidem.",
                        "repo": "Delectus et quia dolor perspiciatis animi minus.",
                        "tag": "Nisi ad dignissimos rerum nulla."
                     },
                     "platform": "Labore adipisci aut dolorum omnis voluptatum.",
                     "sha256_oci_file": {
                        "file": "Ullam quod nobis quidem.",
                        "repo": "Delectus et quia dolor perspiciatis animi minus.",
                        "tag": "Nisi ad dignissimos rerum nulla."
                     },
                     "sha256_url": "http://wehnercruickshank.net/claudie",
                     "url": "http://marks.biz/delfina"
                  }
               ],
               "git_sha": "u24",
               "images": [
                  {
                     "platform": "Dolor eaque harum minima.",
                     "url": "http://ebertzieme.net/d\'angelo"
                  },
                  {
                     "platform": "Dolor eaque harum minima.",
                     "url": "http://ebertzieme.net/d\'angelo"
                  },
                  {
                     "platform": "Dolor eaque harum minima.",
                     "url": "http://ebertzieme.net/d\'angelo"
                  }
               ],
               "plugin_git_sha": "qpk",
               "printed_version": "Et in dolorem incidunt ut unde natus."
            },
            "err_msg": "Nesciunt fugit qui eos quae.",
            "pipeline_build_id": 8884006432911966874,
            "pipeline_end_at": "2001-10-18T06:28:10Z",
            "pipeline_start_at": "1970-05-26T03:25:48Z",
            "pipeline_view_url": "http://pagac.biz/jessyca",
            "pipeline_view_urls": [
               "http://mannwalsh.org/shaniya",
               "http://upton.com/jordan.cremin",
               "http://mrazstehr.info/abner.windler"
            ],
            "status": "PENDING",
            "tekton_status": {
               "pipelines": [
                  {
                     "end_at": "1981-09-04T21:02:25Z",
                     "git_sha": "jxx",
                     "images": [
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        },
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        },
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        },
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        }
                     ],
                     "name": "Commodi et.",
                     "oci_artifacts": [
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        },
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        },
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        },
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        }
                     ],
                     "platform": "Quisquam saepe saepe error consequatur et facilis.",
                     "start_at": "2003-01-02T20:40:11Z",
                     "status": "PROCESSING",
                     "url": "http://priceernser.info/carolyne"
                  },
                  {
                     "end_at": "1981-09-04T21:02:25Z",
                     "git_sha": "jxx",
                     "images": [
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        },
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        },
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        },
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        }
                     ],
                     "name": "Commodi et.",
                     "oci_artifacts": [
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        },
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        },
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        },
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        }
                     ],
                     "platform": "Quisquam saepe saepe error consequatur et facilis.",
                     "start_at": "2003-01-02T20:40:11Z",
                     "status": "PROCESSING",
                     "url": "http://priceernser.info/carolyne"
                  },
                  {
                     "end_at": "1981-09-04T21:02:25Z",
                     "git_sha": "jxx",
                     "images": [
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        },
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        },
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        },
                        {
                           "platform": "Dolor eaque harum minima.",
                           "url": "http://ebertzieme.net/d\'angelo"
                        }
                     ],
                     "name": "Commodi et.",
                     "oci_artifacts": [
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        },
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        },
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        },
                        {
                           "files": [
                              "Quia asperiores.",
                              "Omnis soluta et explicabo nisi."
                           ],
                           "repo": "Rem possimus perferendis est velit pariatur.",
                           "tag": "Doloribus voluptas totam."
                        }
                     ],
                     "platform": "Quisquam saepe saepe error consequatur et facilis.",
                     "start_at": "2003-01-02T20:40:11Z",
                     "status": "PROCESSING",
                     "url": "http://priceernser.info/carolyne"
                  }
               ]
            }
         }
      }
   }' --id 1 --dryrun false
`, os.Args[0])
}

func devbuildRerunUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] devbuild rerun -id INT -dryrun BOOL

Rerun devbuild
    -id INT: ID of build
    -dryrun BOOL: 

Example:
    %[1]s devbuild rerun --id 1 --dryrun false
`, os.Args[0])
}
