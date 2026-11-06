package config

import (
	"strings"
	"testing"
)

func TestRepoAllowlist_Allows(t *testing.T) {
	configured, err := NewRepoAllowlist([]string{
		"pingcap/tidb",
		" TiDBCloud/* ",
	})
	if err != nil {
		t.Fatalf("NewRepoAllowlist() error = %v", err)
	}

	allowed := []string{
		"pingcap/tidb",
		"PingCAP/TiDB",
		"pingcap/TiDB",
		" tidbcloud/cloud-storage-engine ",
		"tidbcloud/anything",
	}
	for _, repo := range allowed {
		if !configured.Allows(repo) {
			t.Errorf("Allows(%q) = false, want true", repo)
		}
	}

	denied := []string{
		"evil/tidb",
		"pingcap/pd",
		"tidbcloud",
		"tidbcloud/",
		"other/cloud-storage-engine",
		"pingcap-inc/tidb",
		"",
	}
	for _, repo := range denied {
		if configured.Allows(repo) {
			t.Errorf("Allows(%q) = true, want false", repo)
		}
	}

	// The zero value is an empty list and allows nothing.
	var empty RepoAllowlist
	if !empty.Empty() {
		t.Error("zero-value RepoAllowlist should be empty")
	}
	for _, repo := range append(allowed, denied...) {
		if empty.Allows(repo) {
			t.Errorf("empty allowlist should not allow %q", repo)
		}
	}
}

// TestRepoAllowlist_DoesNotMatchAcrossSlash pins the wildcard to a single
// repository segment: "owner/*" must not cover nested paths.
func TestRepoAllowlist_DoesNotMatchAcrossSlash(t *testing.T) {
	configured, err := NewRepoAllowlist([]string{"owner/*"})
	if err != nil {
		t.Fatalf("NewRepoAllowlist() error = %v", err)
	}
	if configured.Allows("owner/sub/repo") {
		t.Error(`Allows("owner/sub/repo") = true, want false`)
	}
	if !configured.Allows("owner/repo") {
		t.Error(`Allows("owner/repo") = false, want true`)
	}
}

func TestRepoAllowlist_RejectsBadEntries(t *testing.T) {
	for _, entry := range []string{
		"*/*",
		"*/tidb",
		"pingcap/tid*",
		"pingcap",
		"pingcap/",
		"/tidb",
		"",
		"pingcap/tidb/extra",
		"ping cap/tidb",
	} {
		if _, err := NewRepoAllowlist([]string{entry}); err == nil {
			t.Errorf("NewRepoAllowlist(%q) error = nil, want error", entry)
		}
	}
}

func TestDevBuildValidate_AcceptsExactAndOwnerWildcard(t *testing.T) {
	d := DevBuild{GithubRepoAllowlist: []string{"pingcap/tidb", "tidbcloud/*"}}
	if err := d.Validate(nil); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
}

func TestDevBuildValidate_RejectsBadEntries(t *testing.T) {
	for _, entry := range []string{"*/*", "*/tidb", "pingcap/tid*", "pingcap", "pingcap/", ""} {
		d := DevBuild{GithubRepoAllowlist: []string{"pingcap/tidb", entry}}
		err := d.Validate(nil)
		if err == nil {
			t.Errorf("Validate() with entry %q error = nil, want error", entry)
			continue
		}
		if !strings.Contains(err.Error(), "github_repo_allowlist") {
			t.Errorf("Validate() error = %q, want it to name the key", err)
		}
	}
}

func TestDevBuildValidate_DedupesCaseInsensitively(t *testing.T) {
	d := DevBuild{GithubRepoAllowlist: []string{"PingCAP/TiDB", "pingcap/tidb", "pingcap/tidb"}}
	if err := d.Validate(nil); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
}

func TestDevBuildValidate_EmptySkipsProductCoverage(t *testing.T) {
	d := DevBuild{}
	products := map[string]string{"tidb": "pingcap/tidb", "pd": "pingcap/pd"}
	if err := d.Validate(products); err != nil {
		t.Fatalf("Validate() error = %v, want nil (empty allowlist skips coverage)", err)
	}
}

func TestDevBuildValidate_RequiresProductCoverage(t *testing.T) {
	d := DevBuild{GithubRepoAllowlist: []string{"tidbcloud/*"}}
	products := map[string]string{
		"tidb": "pingcap/tidb",
		"cse":  "tidbcloud/cloud-storage-engine",
	}
	err := d.Validate(products)
	if err == nil {
		t.Fatal("Validate() error = nil, want coverage error")
	}
	msg := err.Error()
	if !strings.Contains(msg, "tidb") || !strings.Contains(msg, "pingcap/tidb") {
		t.Errorf("Validate() error = %q, want it to name the product and the repo", msg)
	}
	if strings.Contains(msg, "cloud-storage-engine") {
		t.Errorf("Validate() error = %q, should not name covered repos", msg)
	}

	// Covering every product value makes the revision valid again.
	d.GithubRepoAllowlist = append(d.GithubRepoAllowlist, "pingcap/tidb")
	if err := d.Validate(products); err != nil {
		t.Fatalf("Validate() error = %v, want nil", err)
	}
}

func TestServiceValidate_PropagatesAllowlistError(t *testing.T) {
	s := Service{
		DevBuild:       DevBuild{GithubRepoAllowlist: []string{"tidbcloud/*"}},
		ProductRepoMap: map[string]string{"tidb": "pingcap/tidb"},
	}
	err := s.Validate()
	if err == nil {
		t.Fatal("Service.Validate() error = nil, want the allowlist coverage error")
	}
	if !strings.Contains(err.Error(), "github_repo_allowlist") {
		t.Errorf("Service.Validate() error = %q, want it to name the key", err)
	}

	s.DevBuild.GithubRepoAllowlist = nil
	if err := s.Validate(); err != nil {
		t.Fatalf("Service.Validate() error = %v, want nil with an empty allowlist", err)
	}
}
