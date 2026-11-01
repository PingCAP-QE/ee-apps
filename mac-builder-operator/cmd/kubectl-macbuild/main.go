package main

import (
	"context"
	"errors"
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
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "logs" {
		_, _ = fmt.Fprint(stderr, usageText)
		return 2
	}

	fs := flag.NewFlagSet("logs", flag.ContinueOnError)
	fs.SetOutput(stderr)
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
	name, flagArgs, err := splitArgs(args[1:])
	if err != nil {
		_, _ = fmt.Fprint(stderr, usageText)
		return 2
	}
	if err := fs.Parse(flagArgs); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if fs.NArg() != 0 {
		_, _ = fmt.Fprint(stderr, usageText)
		return 2
	}

	client, endpoint, err := buildRequest(opts, name)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
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
		_, _ = fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	resp, err := client.Do(req)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "error: %v\n", err)
		return 1
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		_, _ = fmt.Fprintf(stderr, "error: log server returned %d: %s\n", resp.StatusCode, strings.TrimSpace(string(body)))
		if resp.StatusCode == http.StatusForbidden {
			_, _ = fmt.Fprintln(stderr, "hint: you need get on services/proxy; alternatively run: "+
				"kubectl port-forward svc/"+opts.logService+" 8080:8080 -n "+opts.logNamespace+
				" and use --server http://127.0.0.1:8080")
		}
		return 1
	}
	if _, err := io.Copy(stdout, resp.Body); err != nil {
		_, _ = fmt.Fprintf(stderr, "error: read log: %v\n", err)
		return 1
	}
	return 0
}

// splitArgs separates the single positional build name from flags so users can
// write either `logs <name> -n ns` or `logs -n ns <name>`, matching kubectl.
func splitArgs(args []string) (string, []string, error) {
	valueFlags := map[string]struct{}{
		"-n": {}, "--namespace": {},
		"--tail":        {},
		"--log-service": {}, "--log-namespace": {}, "--log-port": {},
		"--server": {},
	}

	var name string
	flags := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			for _, remaining := range args[i+1:] {
				if name != "" {
					return "", nil, fmt.Errorf("unexpected argument %q", remaining)
				}
				name = remaining
			}
			break
		}
		if strings.HasPrefix(arg, "-") && arg != "-" {
			flags = append(flags, arg)
			if strings.Contains(arg, "=") {
				continue
			}
			if _, ok := valueFlags[arg]; ok {
				if i+1 >= len(args) {
					return "", nil, fmt.Errorf("flag %s requires a value", arg)
				}
				i++
				flags = append(flags, args[i])
			}
			continue
		}
		if name != "" {
			return "", nil, fmt.Errorf("unexpected argument %q", arg)
		}
		name = arg
	}
	if name == "" {
		return "", nil, fmt.Errorf("build name is required")
	}
	return name, flags, nil
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
