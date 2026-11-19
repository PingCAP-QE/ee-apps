package main

import (
	"errors"
	"flag"
	"net/http"
	"time"

	cli "github.com/PingCAP-QE/ee-apps/dl/gen/http/cli/server"
	goahttp "goa.design/goa/v3/http"
	goa "goa.design/goa/v3/pkg"
)

func doHTTP(scheme, host string, timeout int, debug bool) (goa.Endpoint, any, error) {
	if args := flag.Args(); debug && len(args) > 0 && args[0] == "shares" {
		return nil, nil, errors.New("verbose logging is disabled for share management")
	}
	var (
		doer goahttp.Doer
	)
	{
		doer = &http.Client{Timeout: time.Duration(timeout) * time.Second}
		if debug {
			doer = goahttp.NewDebugDoer(doer)
		}
	}

	return cli.ParseEndpoint(
		scheme,
		host,
		doer,
		goahttp.RequestEncoder,
		goahttp.ResponseDecoder,
		debug,
	)
}

func httpUsageCommands() []string {
	return cli.UsageCommands()
}

func httpUsageExamples() string {
	return cli.UsageExamples()
}
