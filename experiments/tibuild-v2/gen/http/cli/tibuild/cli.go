// Code generated by goa v3.20.0, DO NOT EDIT.
//
// tibuild HTTP client CLI support package
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/tibuild/design

package cli

import (
	"flag"
	"fmt"
	"net/http"
	"os"

	artifactc "github.com/PingCAP-QE/ee-apps/tibuild/gen/http/artifact/client"
	devbuildc "github.com/PingCAP-QE/ee-apps/tibuild/gen/http/devbuild/client"
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
      "ImageSyncRequest": {
         "source": "Culpa possimus.",
         "target": "Perferendis nisi non quia debitis."
      }
   }'` + "\n" +
		os.Args[0] + ` devbuild list --size 9048917590767144830 --offset 2393528839850969203 --hotfix false --created-by "Fugiat voluptatibus quia."` + "\n" +
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
		devbuildListSizeFlag      = devbuildListFlags.String("size", "10", "")
		devbuildListOffsetFlag    = devbuildListFlags.String("offset", "", "")
		devbuildListHotfixFlag    = devbuildListFlags.String("hotfix", "", "")
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
				data, err = devbuildc.BuildListPayload(*devbuildListSizeFlag, *devbuildListOffsetFlag, *devbuildListHotfixFlag, *devbuildListCreatedByFlag)
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
      "ImageSyncRequest": {
         "source": "Culpa possimus.",
         "target": "Perferendis nisi non quia debitis."
      }
   }'
`, os.Args[0])
}

// devbuildUsage displays the usage of the devbuild command and its subcommands.
func devbuildUsage() {
	fmt.Fprintf(os.Stderr, `The devbuild service provides operations to manage dev builds.
Usage:
    %[1]s [globalflags] devbuild COMMAND [flags]

COMMAND:
    list: List devbuild
    create: Create and trigger devbuild
    get: Get devbuild
    update: Update devbuild status
    rerun: Rerun devbuild

Additional help:
    %[1]s devbuild COMMAND --help
`, os.Args[0])
}
func devbuildListUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] devbuild list -size INT -offset INT -hotfix BOOL -created-by STRING

List devbuild
    -size INT: 
    -offset INT: 
    -hotfix BOOL: 
    -created-by STRING: 

Example:
    %[1]s devbuild list --size 9048917590767144830 --offset 2393528839850969203 --hotfix false --created-by "Fugiat voluptatibus quia."
`, os.Args[0])
}

func devbuildCreateUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] devbuild create -body JSON -dryrun BOOL

Create and trigger devbuild
    -body JSON: 
    -dryrun BOOL: 

Example:
    %[1]s devbuild create --body '{
      "createdBy": "mortimer@herzogfranecki.biz",
      "request": {
         "buildEnv": "Sunt nihil quia numquam suscipit corrupti qui.",
         "builderImg": "Sint ut blanditiis.",
         "edition": "enterprise",
         "features": "Aperiam natus in ut quae accusantium.",
         "gitRef": "Numquam possimus possimus ipsum rerum unde.",
         "githubRepo": "Numquam neque reiciendis quaerat.",
         "isHotfix": true,
         "isPushGCR": true,
         "pipelineEngine": "tekton",
         "pluginGitRef": "Laboriosam esse dicta.",
         "product": "tidb",
         "productBaseImg": "Tempore ut dolores.",
         "productDockerfile": "Iusto suscipit.",
         "targetImg": "Et aut error doloremque non itaque.",
         "version": "Voluptas quia reprehenderit fugit quo debitis numquam."
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
    %[1]s devbuild get --id 1 --sync false
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
      "DevBuild": {
         "id": 8670468037696883245,
         "meta": {
            "createdAt": "Nihil soluta aut adipisci est.",
            "createdBy": "Qui necessitatibus possimus ab quos facere.",
            "updatedAt": "Repudiandae voluptatem rem earum aut at nulla."
         },
         "spec": {
            "buildEnv": "Natus totam esse maxime aliquid sunt.",
            "builderImg": "At illo voluptas dolor.",
            "edition": "community",
            "features": "Beatae sunt nesciunt amet autem.",
            "gitHash": "Sit accusamus aspernatur aut laboriosam.",
            "gitRef": "Perspiciatis perspiciatis atque inventore id.",
            "githubRepo": "Ut libero magnam sapiente dolores qui.",
            "isHotfix": false,
            "isPushGCR": true,
            "pipelineEngine": "jenkins",
            "pluginGitRef": "Deleniti at consequatur doloribus culpa velit et.",
            "product": "pd",
            "productBaseImg": "Voluptas hic.",
            "productDockerfile": "Ut eum iusto id officiis.",
            "targetImg": "Laboriosam ut perspiciatis porro.",
            "version": "Distinctio aliquid eum."
         },
         "status": {
            "buildReport": {
               "binaries": [
                  {
                     "component": "Neque velit maiores culpa rerum accusamus.",
                     "ociFile": {
                        "file": "Consectetur et officia necessitatibus et necessitatibus sint.",
                        "repo": "Enim minima et vel qui nulla qui.",
                        "tag": "Maiores aut rerum."
                     },
                     "platform": "Similique nesciunt dolorum quisquam qui odio.",
                     "sha256OciFile": {
                        "file": "Consectetur et officia necessitatibus et necessitatibus sint.",
                        "repo": "Enim minima et vel qui nulla qui.",
                        "tag": "Maiores aut rerum."
                     },
                     "sha256URL": "Praesentium facilis corrupti ullam impedit incidunt.",
                     "url": "Aut quas pariatur qui."
                  },
                  {
                     "component": "Neque velit maiores culpa rerum accusamus.",
                     "ociFile": {
                        "file": "Consectetur et officia necessitatibus et necessitatibus sint.",
                        "repo": "Enim minima et vel qui nulla qui.",
                        "tag": "Maiores aut rerum."
                     },
                     "platform": "Similique nesciunt dolorum quisquam qui odio.",
                     "sha256OciFile": {
                        "file": "Consectetur et officia necessitatibus et necessitatibus sint.",
                        "repo": "Enim minima et vel qui nulla qui.",
                        "tag": "Maiores aut rerum."
                     },
                     "sha256URL": "Praesentium facilis corrupti ullam impedit incidunt.",
                     "url": "Aut quas pariatur qui."
                  }
               ],
               "gitHash": "Harum repellat qui eos est velit.",
               "images": [
                  {
                     "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                     "url": "Laborum iste nobis omnis quae."
                  },
                  {
                     "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                     "url": "Laborum iste nobis omnis quae."
                  },
                  {
                     "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                     "url": "Laborum iste nobis omnis quae."
                  },
                  {
                     "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                     "url": "Laborum iste nobis omnis quae."
                  }
               ],
               "pluginGitHash": "Assumenda itaque necessitatibus eligendi qui qui illum.",
               "printedVersion": "Aliquam quia eum quia id qui."
            },
            "errMsg": "Fugiat sint aut minus aperiam quod.",
            "pipelineBuildID": 6941289464511149,
            "pipelineEndAt": "Deleniti natus reprehenderit nihil animi hic fugiat.",
            "pipelineStartAt": "Accusamus velit ipsam.",
            "pipelineViewURL": "Enim harum dolorum.",
            "pipelineViewURLs": [
               "Illo blanditiis totam in.",
               "Dignissimos tempore aut."
            ],
            "status": "PENDING",
            "tektonStatus": {
               "pipelines": [
                  {
                     "endAt": "Excepturi ipsa veniam aspernatur quae.",
                     "gitHash": "Voluptatem enim aliquam.",
                     "images": [
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        },
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        },
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        },
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        }
                     ],
                     "name": "Dolores id quibusdam impedit corrupti veritatis.",
                     "ociArtifacts": [
                        {
                           "files": [
                              "Temporibus totam quae debitis odio hic.",
                              "Atque eos consequatur laudantium.",
                              "Vitae iste.",
                              "Odit blanditiis magni eaque quas eos voluptatem."
                           ],
                           "repo": "Quam necessitatibus optio ipsam autem vel veniam.",
                           "tag": "Provident qui."
                        },
                        {
                           "files": [
                              "Temporibus totam quae debitis odio hic.",
                              "Atque eos consequatur laudantium.",
                              "Vitae iste.",
                              "Odit blanditiis magni eaque quas eos voluptatem."
                           ],
                           "repo": "Quam necessitatibus optio ipsam autem vel veniam.",
                           "tag": "Provident qui."
                        },
                        {
                           "files": [
                              "Temporibus totam quae debitis odio hic.",
                              "Atque eos consequatur laudantium.",
                              "Vitae iste.",
                              "Odit blanditiis magni eaque quas eos voluptatem."
                           ],
                           "repo": "Quam necessitatibus optio ipsam autem vel veniam.",
                           "tag": "Provident qui."
                        }
                     ],
                     "platform": "Quis quia vitae non ratione.",
                     "startAt": "Minus sapiente doloremque rerum quasi.",
                     "status": "PROCESSING",
                     "url": "Saepe rerum tenetur vero eveniet."
                  },
                  {
                     "endAt": "Excepturi ipsa veniam aspernatur quae.",
                     "gitHash": "Voluptatem enim aliquam.",
                     "images": [
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        },
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        },
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        },
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        }
                     ],
                     "name": "Dolores id quibusdam impedit corrupti veritatis.",
                     "ociArtifacts": [
                        {
                           "files": [
                              "Temporibus totam quae debitis odio hic.",
                              "Atque eos consequatur laudantium.",
                              "Vitae iste.",
                              "Odit blanditiis magni eaque quas eos voluptatem."
                           ],
                           "repo": "Quam necessitatibus optio ipsam autem vel veniam.",
                           "tag": "Provident qui."
                        },
                        {
                           "files": [
                              "Temporibus totam quae debitis odio hic.",
                              "Atque eos consequatur laudantium.",
                              "Vitae iste.",
                              "Odit blanditiis magni eaque quas eos voluptatem."
                           ],
                           "repo": "Quam necessitatibus optio ipsam autem vel veniam.",
                           "tag": "Provident qui."
                        },
                        {
                           "files": [
                              "Temporibus totam quae debitis odio hic.",
                              "Atque eos consequatur laudantium.",
                              "Vitae iste.",
                              "Odit blanditiis magni eaque quas eos voluptatem."
                           ],
                           "repo": "Quam necessitatibus optio ipsam autem vel veniam.",
                           "tag": "Provident qui."
                        }
                     ],
                     "platform": "Quis quia vitae non ratione.",
                     "startAt": "Minus sapiente doloremque rerum quasi.",
                     "status": "PROCESSING",
                     "url": "Saepe rerum tenetur vero eveniet."
                  },
                  {
                     "endAt": "Excepturi ipsa veniam aspernatur quae.",
                     "gitHash": "Voluptatem enim aliquam.",
                     "images": [
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        },
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        },
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        },
                        {
                           "platform": "Ut rerum dolorum aspernatur necessitatibus.",
                           "url": "Laborum iste nobis omnis quae."
                        }
                     ],
                     "name": "Dolores id quibusdam impedit corrupti veritatis.",
                     "ociArtifacts": [
                        {
                           "files": [
                              "Temporibus totam quae debitis odio hic.",
                              "Atque eos consequatur laudantium.",
                              "Vitae iste.",
                              "Odit blanditiis magni eaque quas eos voluptatem."
                           ],
                           "repo": "Quam necessitatibus optio ipsam autem vel veniam.",
                           "tag": "Provident qui."
                        },
                        {
                           "files": [
                              "Temporibus totam quae debitis odio hic.",
                              "Atque eos consequatur laudantium.",
                              "Vitae iste.",
                              "Odit blanditiis magni eaque quas eos voluptatem."
                           ],
                           "repo": "Quam necessitatibus optio ipsam autem vel veniam.",
                           "tag": "Provident qui."
                        },
                        {
                           "files": [
                              "Temporibus totam quae debitis odio hic.",
                              "Atque eos consequatur laudantium.",
                              "Vitae iste.",
                              "Odit blanditiis magni eaque quas eos voluptatem."
                           ],
                           "repo": "Quam necessitatibus optio ipsam autem vel veniam.",
                           "tag": "Provident qui."
                        }
                     ],
                     "platform": "Quis quia vitae non ratione.",
                     "startAt": "Minus sapiente doloremque rerum quasi.",
                     "status": "PROCESSING",
                     "url": "Saepe rerum tenetur vero eveniet."
                  }
               ]
            }
         }
      }
   }' --id 1 --dryrun true
`, os.Args[0])
}

func devbuildRerunUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] devbuild rerun -id INT -dryrun BOOL

Rerun devbuild
    -id INT: ID of build
    -dryrun BOOL: 

Example:
    %[1]s devbuild rerun --id 1 --dryrun true
`, os.Args[0])
}
