// Code generated by goa v3.14.1, DO NOT EDIT.
//
// server HTTP client CLI support package
//
// Command:
// $ goa gen github.com/PingCAP-QE/ee-apps/dl/design

package cli

import (
	"flag"
	"fmt"
	"net/http"
	"os"

	ks3c "github.com/PingCAP-QE/ee-apps/dl/gen/http/ks3/client"
	ocic "github.com/PingCAP-QE/ee-apps/dl/gen/http/oci/client"
	goahttp "goa.design/goa/v3/http"
	goa "goa.design/goa/v3/pkg"
)

// UsageCommands returns the set of commands and sub-commands using the format
//
//	command (subcommand1|subcommand2|...)
func UsageCommands() string {
	return `oci (list-files|download-file)
ks3 download-object
`
}

// UsageExamples produces an example of a valid invocation of the CLI tool.
func UsageExamples() string {
	return os.Args[0] + ` oci list-files --repository "Est et libero voluptatibus omnis molestiae." --tag "Excepturi perferendis dolores voluptas eius non."` + "\n" +
		os.Args[0] + ` ks3 download-object --bucket "Omnis est natus exercitationem aliquid tempora cumque." --key "Reiciendis eligendi magnam officiis recusandae est fugiat."` + "\n" +
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
		ociFlags = flag.NewFlagSet("oci", flag.ContinueOnError)

		ociListFilesFlags          = flag.NewFlagSet("list-files", flag.ExitOnError)
		ociListFilesRepositoryFlag = ociListFilesFlags.String("repository", "REQUIRED", "OCI artifact repository")
		ociListFilesTagFlag        = ociListFilesFlags.String("tag", "REQUIRED", "")

		ociDownloadFileFlags          = flag.NewFlagSet("download-file", flag.ExitOnError)
		ociDownloadFileRepositoryFlag = ociDownloadFileFlags.String("repository", "REQUIRED", "OCI artifact repository")
		ociDownloadFileFileFlag       = ociDownloadFileFlags.String("file", "REQUIRED", "")
		ociDownloadFileTagFlag        = ociDownloadFileFlags.String("tag", "REQUIRED", "")

		ks3Flags = flag.NewFlagSet("ks3", flag.ContinueOnError)

		ks3DownloadObjectFlags      = flag.NewFlagSet("download-object", flag.ExitOnError)
		ks3DownloadObjectBucketFlag = ks3DownloadObjectFlags.String("bucket", "REQUIRED", "bucket name")
		ks3DownloadObjectKeyFlag    = ks3DownloadObjectFlags.String("key", "REQUIRED", "object key")
	)
	ociFlags.Usage = ociUsage
	ociListFilesFlags.Usage = ociListFilesUsage
	ociDownloadFileFlags.Usage = ociDownloadFileUsage

	ks3Flags.Usage = ks3Usage
	ks3DownloadObjectFlags.Usage = ks3DownloadObjectUsage

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
		case "oci":
			svcf = ociFlags
		case "ks3":
			svcf = ks3Flags
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
		case "oci":
			switch epn {
			case "list-files":
				epf = ociListFilesFlags

			case "download-file":
				epf = ociDownloadFileFlags

			}

		case "ks3":
			switch epn {
			case "download-object":
				epf = ks3DownloadObjectFlags

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
		case "oci":
			c := ocic.NewClient(scheme, host, doer, enc, dec, restore)
			switch epn {
			case "list-files":
				endpoint = c.ListFiles()
				data, err = ocic.BuildListFilesPayload(*ociListFilesRepositoryFlag, *ociListFilesTagFlag)
			case "download-file":
				endpoint = c.DownloadFile()
				data, err = ocic.BuildDownloadFilePayload(*ociDownloadFileRepositoryFlag, *ociDownloadFileFileFlag, *ociDownloadFileTagFlag)
			}
		case "ks3":
			c := ks3c.NewClient(scheme, host, doer, enc, dec, restore)
			switch epn {
			case "download-object":
				endpoint = c.DownloadObject()
				data, err = ks3c.BuildDownloadObjectPayload(*ks3DownloadObjectBucketFlag, *ks3DownloadObjectKeyFlag)
			}
		}
	}
	if err != nil {
		return nil, nil, err
	}

	return endpoint, data, nil
}

// ociUsage displays the usage of the oci command and its subcommands.
func ociUsage() {
	fmt.Fprintf(os.Stderr, `OCI artifacts download service
Usage:
    %[1]s [globalflags] oci COMMAND [flags]

COMMAND:
    list-files: ListFiles implements list-files.
    download-file: DownloadFile implements download-file.

Additional help:
    %[1]s oci COMMAND --help
`, os.Args[0])
}
func ociListFilesUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] oci list-files -repository STRING -tag STRING

ListFiles implements list-files.
    -repository STRING: OCI artifact repository
    -tag STRING: 

Example:
    %[1]s oci list-files --repository "Est et libero voluptatibus omnis molestiae." --tag "Excepturi perferendis dolores voluptas eius non."
`, os.Args[0])
}

func ociDownloadFileUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] oci download-file -repository STRING -file STRING -tag STRING

DownloadFile implements download-file.
    -repository STRING: OCI artifact repository
    -file STRING: 
    -tag STRING: 

Example:
    %[1]s oci download-file --repository "Dolores qui voluptas autem illo cum." --file "Quis maiores hic et commodi aut." --tag "Corrupti qui qui iusto."
`, os.Args[0])
}

// ks3Usage displays the usage of the ks3 command and its subcommands.
func ks3Usage() {
	fmt.Fprintf(os.Stderr, `OCI artifacts download service
Usage:
    %[1]s [globalflags] ks3 COMMAND [flags]

COMMAND:
    download-object: DownloadObject implements download-object.

Additional help:
    %[1]s ks3 COMMAND --help
`, os.Args[0])
}
func ks3DownloadObjectUsage() {
	fmt.Fprintf(os.Stderr, `%[1]s [flags] ks3 download-object -bucket STRING -key STRING

DownloadObject implements download-object.
    -bucket STRING: bucket name
    -key STRING: object key

Example:
    %[1]s ks3 download-object --bucket "Omnis est natus exercitationem aliquid tempora cumque." --key "Reiciendis eligendi magnam officiis recusandae est fugiat."
`, os.Args[0])
}