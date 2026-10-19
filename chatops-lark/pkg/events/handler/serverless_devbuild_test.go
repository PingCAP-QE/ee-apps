package handler

import "testing"

func TestServerlessDevbuildCommands(t *testing.T) {
	for _, product := range []string{"tidb-cse", "pd-cse", "tidb-operator-cse", "cassandra-on-mysql", "qdrant-on-tidb"} {
		base := []string{"--product", product, "--version", "v99.0.0", "--gitRef", "pull/42"}
		for _, platform := range []string{"linux", "linux/amd64", "linux/arm64"} {
			p, err := parseCommandDevbuildTrigger(append(append([]string{}, base...), "--platform", platform))
			if err != nil {
				t.Fatalf("%s %s: %v", product, platform, err)
			}
			if p.engine != "tekton" || p.version != "v99.0.0" {
				t.Fatalf("lost engine/version: %+v", p)
			}
		}
		for _, args := range [][]string{{"--platform", "darwin"}, {"--engine", "jenkins"}, {"--edition", "enterprise"}} {
			if _, err := parseCommandDevbuildTrigger(append(append([]string{}, base...), args...)); err == nil {
				t.Fatalf("accepted %s %v", product, args)
			}
		}
	}
}
