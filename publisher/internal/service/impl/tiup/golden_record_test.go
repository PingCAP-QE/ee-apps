//go:build golden

// Package tiup's golden recorder captures the delivery plan produced by the
// pre-migration publisher implementation, so that the tibuild-v2 port can be
// proven behaviourally identical.
//
// It is excluded from normal builds and test runs by the `golden` build tag.
// Run it from the publisher module:
//
//	crane/network access required for the OCI artifact configs.
//
//	GOLDEN_DELIVERY_CONFIG=$HOME/apps/github.com/PingCAP-QE/artifacts/packages/delivery.yaml \
//	GOLDEN_OUT=../tibuild-v2/pkg/delivery/testdata/delivery_golden.yaml \
//	go test -tags golden -run TestRecordGolden -v ./internal/service/impl/tiup
//
// The recorder deliberately calls the same two functions the HTTP handler
// calls (analyzeTiupDeliveries, then analyzeTiupFromOciArtifactUrl), so the
// recorded steps are exactly what `POST /tiup/delivery-by-rules` would enqueue.
package tiup

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/PingCAP-QE/ee-apps/publisher/internal/service/impl/share"
	"github.com/PingCAP-QE/ee-apps/publisher/pkg/config"
)

// goldenArtifactURLs is the recorded corpus: nightly, pre, classic GA,
// nextgen semver, nextgen branch (feature-fts), and a non-matching branch tag
// (release-8.5) that must yield zero steps. Both linux and darwin platforms
// are represented.
var goldenArtifactURLs = []string{
	// nightly rule → [staging, prod]
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:master_linux_amd64",
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:master_darwin_arm64",
	// pre (release branch) rule → [staging]
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:release-8.5_linux_amd64",
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:release-8.5_darwin_arm64",
	// FTS rule → [staging]
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:feature-fts_linux_amd64",
	// Rust SQL node rule → [staging], fixed version
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:hparser-integration-rust_linux_amd64",
	// classic EA/GA rule → [staging, prod], version_regex_replace "$1"
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:v8.5.6_linux_amd64",
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:v8.5.6_darwin_arm64",
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tiflash/package:v8.5.6_linux_amd64",
	// must not match any rule (no network fetch expected)
	"us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/tidb/package:v8.5.6_windows_amd64",
}

// NOTE: `us-docker.pkg.dev/pingcap-testing-account/tidbx/...` artifacts are not
// anonymously readable (GAR returns 403 on the anonymous token request), and
// the pre-migration publisher fetches configs with an explicitly empty
// credential. Nextgen rule coverage therefore lives in the port's table-driven
// rule tests instead of this recorded corpus. Making tibuild-v2 able to read
// the tidbx repository is a Phase 3 prerequisite (see working-notes.md).

type goldenStep struct {
	Target      string `yaml:"target" json:"target"`
	Name        string `yaml:"name" json:"name"`
	Version     string `yaml:"version" json:"version"`
	OS          string `yaml:"os" json:"os"`
	Arch        string `yaml:"arch" json:"arch"`
	Description string `yaml:"description,omitempty" json:"description,omitempty"`
	EntryPoint  string `yaml:"entry_point" json:"entry_point"`
	Standalone  bool   `yaml:"standalone" json:"standalone"`
	File        string `yaml:"file" json:"file"`
	Nightly     bool   `yaml:"nightly" json:"nightly"`

	// Provenance only: the OCI manifest digest is resolved from a mutable tag,
	// so it is recorded for evidence but intentionally excluded from golden
	// comparison.
	SourceRepo   string `yaml:"source_repo" json:"source_repo"`
	SourceTag    string `yaml:"source_tag" json:"source_tag"`
	SourceDigest string `yaml:"source_digest,omitempty" json:"source_digest,omitempty"`
}

type goldenCase struct {
	ArtifactURL string `yaml:"artifact_url" json:"artifact_url"`
	// ArtifactConfig is the OCI artifact config snapshot the plan was computed
	// from. Keeping it makes the golden test hermetic (no registry access).
	ArtifactConfig map[string]any `yaml:"artifact_config" json:"artifact_config"`
	Steps          []goldenStep   `yaml:"steps" json:"steps"`
}

type goldenFile struct {
	GeneratedAt string `yaml:"generated_at"`
	Source      string `yaml:"source"`
	// DeliveryConfigSHA256 ties the corpus to the delivery.yaml revision used.
	DeliveryConfigSHA256 string       `yaml:"delivery_config_sha256"`
	Cases                []goldenCase `yaml:"cases"`
}

func TestRecordGolden(t *testing.T) {
	deliveryCfgPath := os.Getenv("GOLDEN_DELIVERY_CONFIG")
	outPath := os.Getenv("GOLDEN_OUT")
	if deliveryCfgPath == "" || outPath == "" {
		t.Skip("set GOLDEN_DELIVERY_CONFIG and GOLDEN_OUT to record the golden dataset")
	}

	deliveryCfg, err := config.Load[DeliveryConfig](deliveryCfgPath)
	if err != nil {
		t.Fatalf("load delivery config: %v", err)
	}
	cfgBytes, err := os.ReadFile(deliveryCfgPath)
	if err != nil {
		t.Fatalf("read delivery config: %v", err)
	}

	out := goldenFile{
		GeneratedAt:          "2026-10-30",
		Source:               "publisher/internal/service/impl/tiup (pre-migration)",
		DeliveryConfigSHA256: fmt.Sprintf("%x", sha256.Sum256(cfgBytes)),
	}

	for _, url := range goldenArtifactURLs {
		t.Run(url, func(t *testing.T) {
			c := goldenCase{ArtifactURL: url, Steps: []goldenStep{}}

			payloads, err := analyzeTiupDeliveries(url, deliveryCfg.TiupPublishRules)
			if err != nil {
				t.Fatalf("analyzeTiupDeliveries: %v", err)
			}

			for _, p := range payloads {
				reqs, err := analyzeTiupFromOciArtifactUrl(p.ArtifactURL, p.TiupMirror)
				if err != nil {
					t.Fatalf("analyzeTiupFromOciArtifactUrl(%s): %v", p.ArtifactURL, err)
				}
				// Mirror the version override performed by RequestToPublish.
				if p.Version != nil && *p.Version != "" {
					for i := range reqs {
						reqs[i].Publish.Version = *p.Version
					}
				}

				// The artifact config is identical for every mirror of the same
				// artifact, so recording it once is enough; assert consistency.
				repo, tag, err := share.SplitRepoAndTag(p.ArtifactURL)
				if err != nil {
					t.Fatalf("split %s: %v", p.ArtifactURL, err)
				}
				cfg, digest, err := share.FetchOCIArtifactConfig(repo, tag)
				if err != nil {
					t.Fatalf("fetch config %s: %v", p.ArtifactURL, err)
				}
				if c.ArtifactConfig == nil {
					c.ArtifactConfig = cfg
				} else if !configsEqual(c.ArtifactConfig, cfg) {
					t.Fatalf("artifact config changed between targets for %s", p.ArtifactURL)
				}

				for _, r := range reqs {
					step := goldenStep{
						Target:       p.TiupMirror,
						Name:         r.Publish.Name,
						Version:      r.Publish.Version,
						OS:           r.Publish.Os,
						Arch:         r.Publish.Arch,
						File:         r.From.Oci.File,
						Nightly:      tiupVersionRegex.MatchString(r.Publish.Version),
						SourceRepo:   repo,
						SourceTag:    tag,
						SourceDigest: digest,
					}
					if r.Publish.Description != nil {
						step.Description = *r.Publish.Description
					}
					if r.Publish.EntryPoint != nil {
						step.EntryPoint = *r.Publish.EntryPoint
					}
					if r.Publish.Standalone != nil {
						step.Standalone = *r.Publish.Standalone
					}
					c.Steps = append(c.Steps, step)
				}
			}

			sort.Slice(c.Steps, func(i, j int) bool {
				a, b := c.Steps[i], c.Steps[j]
				if a.Target != b.Target {
					return a.Target < b.Target
				}
				return a.Name < b.Name
			})
			out.Cases = append(out.Cases, c)
		})
	}

	bs, err := yaml.Marshal(out)
	if err != nil {
		t.Fatalf("marshal golden: %v", err)
	}
	header := []byte("# Generated by publisher/internal/service/impl/tiup/golden_record_test.go (build tag: golden).\n" +
		"# Do not edit by hand: re-record when the delivery rules change intentionally.\n")
	if err := os.WriteFile(outPath, append(header, bs...), 0o644); err != nil {
		t.Fatalf("write %s: %v", outPath, err)
	}
	t.Logf("recorded %d cases to %s", len(out.Cases), outPath)
}

func configsEqual(a, b map[string]any) bool {
	ab, err1 := json.Marshal(a)
	bb, err2 := json.Marshal(b)
	return err1 == nil && err2 == nil && string(ab) == string(bb)
}
