package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

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

func TestServerlessDevbuildRequestCompatibility(t *testing.T) {
	for _, product := range []string{"tidb-cse", "pd-cse", "tidb-operator-cse", "cassandra-on-mysql", "qdrant-on-tidb"} {
		t.Run(product, func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				request := body["request"].(map[string]any)
				for key, want := range map[string]any{"product": product, "version": "v99.0.0", "gitRef": "pull/42", "githubRepo": "custom-owner/custom-name", "platform": "linux", "pipelineEngine": "tekton"} {
					if request[key] != want {
						t.Errorf("%s: got %v, want %v", key, request[key], want)
					}
				}
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"id":123}`))
			}))
			defer server.Close()
			ctx := context.WithValue(context.Background(), cfgKeyDevBuildURL, server.URL+"/api/v2/devbuilds")
			ctx = context.WithValue(ctx, ctxKeyLarkSenderEmail, "user@example.com")
			base := []string{"--product", product, "--version", "v99.0.0", "--gitRef", "pull/42", "--githubRepo", "custom-owner/custom-name"}
			result, err := runCommandDevbuildTrigger(ctx, append(append([]string{}, base...), "--platform", "linux"))
			if err != nil || result != "build id is 123" {
				t.Fatalf("trigger: %q %v", result, err)
			}
			_, err = runCommandDevbuildTrigger(ctx, append(append([]string{}, base...), "--platform", "darwin"))
			if err == nil || calls != 1 {
				t.Fatalf("invalid request reached API: calls=%d err=%v", calls, err)
			}
		})
	}
}
