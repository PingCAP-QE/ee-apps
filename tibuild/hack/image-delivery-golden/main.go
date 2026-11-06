// Command image-delivery-golden records the expansion of the image delivery
// rules with the *current* implementation (the deno script
// gen-delivery-image-commands.ts in PingCAP-QE/artifacts) and writes it as the
// golden dataset consumed by pkg/delivery tests.
//
// The recorded plans are the contract the Go implementation must reproduce, so
// this tool deliberately keeps its own decoding of the rules file: if the Go
// types dropped a field (for example constant_tags), the golden plans would no
// longer match and the test would fail.
//
// Usage (requires deno in PATH):
//
//	go run ./hack/image-delivery-golden \
//	    -delivery-yaml ../../artifacts/packages/delivery.yaml \
//	    -script ../../artifacts/packages/scripts/gen-delivery-image-commands.ts \
//	    -out pkg/delivery/testdata/image_delivery_golden.yaml
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"gopkg.in/yaml.v3"
)

// candidateTags covers every tags_regex family in packages/delivery.yaml:
// trunk/nightly, enterprise, semver pre/GA/rc, release branches, sha suffixes,
// FTS, nextgen and calendar-style nextgen tags.
var candidateTags = []string{
	"master",
	"main",
	"master-experiment",
	"main-experiment",
	"master-enterprise",
	"main-enterprise",
	"release-8.5",
	"release-8.5-beta.1",
	"release-8.5-abcdef1",
	"release-nextgen",
	"release-nextgen-abc123",
	"release-nextgen-deadbeef",
	"master-abc1234",
	"master-nextgen",
	"main-nextgen",
	"dedicated-nextgen",
	"cloud-engine-nextgen",
	"feature-fts-nextgen",
	"feature-fts-1-abc-nextgen",
	"v1.6.0",
	"v2.0.0",
	"v2.0.0-pre",
	"v8.5.0",
	"v8.5.0-pre",
	"v8.5.0-pre-1-gabcdef",
	"v8.5.0-beta.1",
	"v8.5.0-beta.1.pre",
	"v8.5.0-beta.1.pre-1-gabcdef",
	"v8.5.0-beta.1-enterprise",
	"v8.5.0-enterprise",
	"v8.5.0-rc.1",
	"v8.5.0-rc.1.pre",
	"v8.5.0-rc.1-enterprise",
	"v8.5.0-release.1",
	"v8.5.0-release.1-12-abcdef",
	"v8.5.6-20260228-abcdef",
	"v26.3.1",
	"v26.3.1-2-gabcdef12",
	"v26.3.1-2-gabcdef12-nextgen",
	"v26.3.1-nextgen",
	"v26.3.1-nextgen.202601.3",
	"v26.3.1-nextgen.202601.3-2-gabcdef12",
	"v9.0.0-beta.2.pre",
	"nightly",
	"latest",
	"20240101-abcdef",
}

// goldenFile is the on-disk snapshot. Rules keeps the rules verbatim (as
// decoded generic YAML) so the snapshot stays independent of the Go types
// under test.
type goldenFile struct {
	Source     goldenSource     `yaml:"source"`
	Deprecated []string         `yaml:"deprecated"`
	Rules      map[string][]any `yaml:"rules"`
	Cases      []goldenCase     `yaml:"cases"`
}

type goldenSource struct {
	DeliveryYAML string `yaml:"delivery_yaml"`
	Commit       string `yaml:"commit"`
	SHA256       string `yaml:"sha256"`
	Script       string `yaml:"script"`
	GeneratedBy  string `yaml:"generated_by"`
}

type goldenCase struct {
	Source string       `yaml:"source"`
	Tag    string       `yaml:"tag"`
	Plans  []goldenPlan `yaml:"plans,omitempty"`
}

type goldenPlan struct {
	Target string   `yaml:"target"`
	Dest   string   `yaml:"dest"`
	Tags   []string `yaml:"tags"`
}

type rawConfig struct {
	Deprecated     []string         `yaml:"deprecated"`
	ImageCopyRules map[string][]any `yaml:"image_copy_rules"`
}

func main() {
	var (
		deliveryYAML = flag.String("delivery-yaml", "", "path to artifacts/packages/delivery.yaml")
		script       = flag.String("script", "", "path to artifacts/packages/scripts/gen-delivery-image-commands.ts")
		out          = flag.String("out", "", "output path for the golden dataset")
		concurrency  = flag.Int("concurrency", 8, "parallel deno runs")
	)
	flag.Parse()
	if *deliveryYAML == "" || *script == "" || *out == "" {
		flag.Usage()
		fail(errors.New("-delivery-yaml, -script and -out are required"))
	}

	cfg, err := loadConfig(*deliveryYAML)
	if err != nil {
		fail(err)
	}

	repos := make([]string, 0, len(cfg.ImageCopyRules))
	for repo := range cfg.ImageCopyRules {
		repos = append(repos, repo)
	}
	sort.Strings(repos)

	type job struct {
		repo string
		tag  string
	}
	type result struct {
		plans     []goldenPlan
		err       error
		targetImg []string
	}

	jobs := make([]job, 0, len(repos)*len(candidateTags))
	for _, repo := range repos {
		for _, tag := range candidateTags {
			jobs = append(jobs, job{repo: repo, tag: tag})
		}
	}

	results := make([]result, len(jobs))
	var wg sync.WaitGroup
	sem := make(chan struct{}, *concurrency)
	for i, j := range jobs {
		wg.Add(1)
		go func(i int, j job) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			plans, targets, err := expand(*script, *deliveryYAML, j.repo, j.tag)
			results[i] = result{plans: plans, targetImg: targets, err: err}
		}(i, j)
	}
	wg.Wait()

	raw, err := os.ReadFile(*deliveryYAML)
	if err != nil {
		fail(err)
	}
	sum := sha256.Sum256(raw)
	golden := goldenFile{
		Source: goldenSource{
			DeliveryYAML: filepath.Base(*deliveryYAML),
			Commit:       gitCommit(*deliveryYAML),
			SHA256:       hex.EncodeToString(sum[:]),
			Script:       filepath.Base(*script),
			GeneratedBy:  "hack/image-delivery-golden",
		},
		Deprecated: cfg.Deprecated,
		Rules:      cfg.ImageCopyRules,
	}
	for i, j := range jobs {
		r := results[i]
		if r.err != nil {
			fail(fmt.Errorf("%s:%s: %w", j.repo, j.tag, r.err))
		}
		if err := checkTargets(r.plans, r.targetImg); err != nil {
			fail(fmt.Errorf("%s:%s: %w", j.repo, j.tag, err))
		}
		golden.Cases = append(golden.Cases, goldenCase{
			Source: j.repo,
			Tag:    j.tag,
			Plans:  r.plans,
		})
	}

	payload, err := yaml.Marshal(golden)
	if err != nil {
		fail(err)
	}
	header := "# Code generated by hack/image-delivery-golden; DO NOT EDIT.\n" +
		"# It records how the deno generator (artifacts/packages/scripts/\n" +
		"# gen-delivery-image-commands.ts) expands every image_copy_rules entry of\n" +
		"# artifacts/packages/delivery.yaml. pkg/delivery must reproduce these plans.\n"
	if err := os.MkdirAll(filepath.Dir(*out), 0o755); err != nil {
		fail(err)
	}
	if err := os.WriteFile(*out, append([]byte(header), payload...), 0o644); err != nil {
		fail(err)
	}
	fmt.Printf("wrote %s (%d cases, %d rules)\n", *out, len(golden.Cases), len(golden.Rules))
}

func loadConfig(path string) (*rawConfig, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg rawConfig
	if err := yaml.Unmarshal(content, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if len(cfg.ImageCopyRules) == 0 {
		return nil, fmt.Errorf("%s has no image_copy_rules", path)
	}
	return &cfg, nil
}

// expand runs the deno generator for a single source reference and converts the
// generated shell script back into structured plans.
func expand(script, deliveryYAML, repo, tag string) ([]goldenPlan, []string, error) {
	dir, err := os.MkdirTemp("", "golden")
	if err != nil {
		return nil, nil, err
	}
	defer os.RemoveAll(dir)

	shPath := filepath.Join(dir, "delivery.sh")
	targetsPath := filepath.Join(dir, "targets.yaml")
	cmd := exec.Command("deno", "run", "--allow-read", "--allow-write", script,
		"--image_url="+repo+":"+tag,
		"--yaml_file="+deliveryYAML,
		"--outfile="+shPath,
		"--images_outfile="+targetsPath,
	)
	// The generator logs its decisions on stderr; the exit code and the files
	// are the contract.
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err != nil {
		return nil, nil, fmt.Errorf("deno: %w", err)
	}

	script0, err := os.ReadFile(shPath)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil, nil
	}
	if err != nil {
		return nil, nil, err
	}

	plans, err := parseCommands(string(script0))
	if err != nil {
		return nil, nil, err
	}
	var targets []string
	if content, err := os.ReadFile(targetsPath); err == nil && len(content) > 0 {
		var parsed struct {
			Images []string `yaml:"images"`
		}
		if err := yaml.Unmarshal(content, &parsed); err != nil {
			return nil, nil, fmt.Errorf("parse targets: %w", err)
		}
		targets = parsed.Images
	}
	return plans, targets, nil
}

// parseCommands turns the generated `crane copy` / `crane tag` lines into
// plans. A `crane tag <dest> <tag>` belongs to the plan created by the most
// recent `crane copy` into the same destination repository.
func parseCommands(script string) ([]goldenPlan, error) {
	var plans []goldenPlan
	lastByDest := map[string]int{}
	for _, line := range strings.Split(script, "\n") {
		fields := strings.Fields(strings.TrimSpace(line))
		if len(fields) == 0 {
			continue
		}
		switch {
		case len(fields) == 4 && fields[0] == "crane" && fields[1] == "copy":
			destRepo, tag, err := splitRef(fields[3])
			if err != nil {
				return nil, fmt.Errorf("copy destination %q: %w", fields[3], err)
			}
			plans = append(plans, goldenPlan{Target: targetOf(destRepo), Dest: destRepo, Tags: []string{tag}})
			lastByDest[destRepo] = len(plans) - 1
		case len(fields) == 4 && fields[0] == "crane" && fields[1] == "tag":
			destRepo, _, err := splitRef(fields[2])
			if err != nil {
				return nil, err
			}
			tag := fields[3]
			idx, ok := lastByDest[destRepo]
			if !ok {
				return nil, fmt.Errorf("crane tag for unknown destination %q", destRepo)
			}
			plans[idx].Tags = append(plans[idx].Tags, tag)
		default:
			return nil, fmt.Errorf("unexpected command %q", line)
		}
	}
	return plans, nil
}

// checkTargets verifies the parsed plans against the generator's own
// delivery-target-images.yaml output, so a parsing bug cannot silently become
// the golden contract.
func checkTargets(plans []goldenPlan, want []string) error {
	var got []string
	for _, p := range plans {
		for _, t := range p.Tags {
			got = append(got, p.Dest+":"+t)
		}
	}
	if len(got) != len(want) {
		return fmt.Errorf("target images mismatch: parsed %d, generator wrote %d (%v vs %v)", len(got), len(want), got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			return fmt.Errorf("target image %d: parsed %q, generator wrote %q", i, got[i], want[i])
		}
	}
	return nil
}

func splitRef(ref string) (string, string, error) {
	slash := strings.LastIndex(ref, "/")
	colon := strings.LastIndex(ref, ":")
	if colon <= slash {
		return "", "", fmt.Errorf("reference %q has no tag", ref)
	}
	return ref[:colon], ref[colon+1:], nil
}

// targetOf mirrors the rule the worker uses to shard on registries: the first
// path segment when it looks like a registry host, docker.io otherwise.
func targetOf(dest string) string {
	idx := strings.Index(dest, "/")
	if idx < 0 {
		return "docker.io"
	}
	host := dest[:idx]
	if strings.ContainsAny(host, ".:") || host == "localhost" {
		return host
	}
	return "docker.io"
}

// gitCommit records where the rules came from, so a stale dataset is easy to
// spot. It is best effort: a source tree without git metadata still works.
func gitCommit(path string) string {
	cmd := exec.Command("git", "-C", filepath.Dir(path), "rev-parse", "HEAD")
	out, err := cmd.Output()
	if err != nil {
		return "unknown"
	}
	return strings.TrimSpace(string(out))
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "image-delivery-golden:", err)
	os.Exit(1)
}
