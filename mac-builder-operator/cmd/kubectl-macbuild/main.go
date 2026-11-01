package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"

	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

const usageText = `Usage:
  kubectl macbuild logs <name> [flags]

Flags:
  -n, --namespace string        MacBuild namespace (default "ee-cd")
  -f, --follow                  Follow the log until the build log is complete
      --tail int                Show only the last N lines
      --log-service string      Log server Service (default "mac-builder-operator-logs")
      --log-namespace string    Namespace of the log server Service (default "ee-cd")
      --log-port int            Log server Service port (default 8080)
      --server string           Use a direct server URL (e.g. http://127.0.0.1:8080)
`

type options struct {
	namespace    string
	follow       bool
	tail         int
	logService   string
	logNamespace string
	logPort      int
	server       string
}

func main() {
	if len(os.Args) < 2 || os.Args[1] != "logs" {
		fmt.Fprint(os.Stderr, usageText)
		os.Exit(2)
	}

	fs := flag.NewFlagSet("logs", flag.ExitOnError)
	opts := options{}
	fs.StringVar(&opts.namespace, "namespace", "ee-cd", "MacBuild namespace")
	fs.StringVar(&opts.namespace, "n", "ee-cd", "MacBuild namespace")
	fs.BoolVar(&opts.follow, "follow", false, "Follow the log")
	fs.BoolVar(&opts.follow, "f", false, "Follow the log")
	fs.IntVar(&opts.tail, "tail", 0, "Show only the last N lines")
	fs.StringVar(&opts.logService, "log-service", "mac-builder-operator-logs", "Log server Service")
	fs.StringVar(&opts.logNamespace, "log-namespace", "ee-cd", "Log server namespace")
	fs.IntVar(&opts.logPort, "log-port", 8080, "Log server port")
	fs.StringVar(&opts.server, "server", "", "Direct log server URL")
	_ = fs.Parse(os.Args[2:])

	if fs.NArg() != 1 {
		fmt.Fprint(os.Stderr, usageText)
		os.Exit(2)
	}
	name := fs.Arg(0)

	client, endpoint, err := buildRequest(opts, name)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}

	query := url.Values{}
	if opts.tail > 0 {
		query.Set("tail", strconv.Itoa(opts.tail))
	}
	if opts.follow {
		query.Set("follow", "true")
	}
	if len(query) > 0 {
		endpoint += "?" + query.Encode()
	}

	req, err := http.NewRequestWithContext(context.Background(), http.MethodGet, endpoint, nil)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
	resp, err := client.Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		fmt.Fprintf(os.Stderr, "error: log server returned %d: %s\n", resp.StatusCode, strings.TrimSpace(string(body)))
		if resp.StatusCode == http.StatusForbidden {
			fmt.Fprintln(os.Stderr, "hint: you need get on services/proxy; alternatively run: "+
				"kubectl port-forward svc/"+opts.logService+" 8080:8080 -n "+opts.logNamespace+
				" and use --server http://127.0.0.1:8080")
		}
		os.Exit(1)
	}
	if _, err := io.Copy(os.Stdout, resp.Body); err != nil {
		fmt.Fprintf(os.Stderr, "error: read log: %v\n", err)
		os.Exit(1)
	}
}

func buildRequest(opts options, name string) (*http.Client, string, error) {
	path := fmt.Sprintf("/v1/logs/%s/%s", opts.namespace, name)
	if opts.server != "" {
		return &http.Client{}, strings.TrimRight(opts.server, "/") + path, nil
	}

	loadingRules := clientcmd.NewDefaultClientConfigLoadingRules()
	config, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		loadingRules, &clientcmd.ConfigOverrides{}).ClientConfig()
	if err != nil {
		return nil, "", fmt.Errorf("load kubeconfig: %w", err)
	}
	transport, err := rest.TransportFor(config)
	if err != nil {
		return nil, "", fmt.Errorf("build API transport: %w", err)
	}
	endpoint := fmt.Sprintf(
		"%s/api/v1/namespaces/%s/services/http:%s:%d/proxy%s",
		strings.TrimRight(config.Host, "/"), opts.logNamespace, opts.logService, opts.logPort, path,
	)
	return &http.Client{Transport: transport}, endpoint, nil
}
