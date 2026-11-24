// registry-check validates an authorization registry and prints its coverage
// summary. It does not claim that every deployed route has a registry entry.
package main

import (
	"fmt"
	"io"
	"os"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/registry"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) != 1 {
		fmt.Fprintln(stderr, "usage: registry-check <registry.json>")
		return 2
	}
	data, err := os.ReadFile(args[0])
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	r, err := registry.Parse(data)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}

	fmt.Fprintln(stdout, "registry_version,route_id,host,route,method,path,mode,capability_checks,resource_checks")
	var capabilityRules, resourceRules int
	for _, rule := range r.Rules() {
		capabilityChecks, resourceChecks := 0, 0
		for _, check := range rule.Checks {
			if check.Object != "" {
				capabilityChecks++
			}
			if check.ObjectFrom != "" {
				resourceChecks++
			}
		}
		if capabilityChecks > 0 {
			capabilityRules++
		}
		if resourceChecks > 0 {
			resourceRules++
		}
		fmt.Fprintf(stdout, "%s,%s,%s,%s,%s,%s,%s,%d,%d\n", r.Version(), rule.ID, rule.Host, rule.Route, rule.Method, rule.Path, rule.Mode, capabilityChecks, resourceChecks)
	}
	fmt.Fprintf(stderr, "registry rules: %d; rules with capability checks: %d; rules with resource checks: %d\n", len(r.Rules()), capabilityRules, resourceRules)
	return 0
}
