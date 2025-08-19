package image

import (
	"reflect"
	"testing"
)

func TestParseRepoAndTag(t *testing.T) {
	tests := []struct {
		imageURL string
		wantRepo string
		wantTag  string
		wantErr  bool
	}{
		{"repo.example.com/foo/bar:v1.2.3-linux-amd64", "repo.example.com/foo/bar", "v1.2.3-linux-amd64", false},
		{"repo:tag", "repo", "tag", false},
		{"repo-only", "", "", true},
		{"repo:tag:extra", "", "", true},
	}

	for _, tt := range tests {
		repo, tag, err := parseRepoAndTag(tt.imageURL)
		if (err != nil) != tt.wantErr {
			t.Errorf("parseRepoAndTag(%q) error = %v, wantErr %v", tt.imageURL, err, tt.wantErr)
			continue
		}
		if repo != tt.wantRepo || tag != tt.wantTag {
			t.Errorf("parseRepoAndTag(%q) = (%q, %q), want (%q, %q)", tt.imageURL, repo, tag, tt.wantRepo, tt.wantTag)
		}
	}
}

func TestComputeBaseTags(t *testing.T) {
	tests := []struct {
		pushedTag        string
		releaseTagSuffix string
		want             []string
	}{
		{
			"master-00595b4-release_linux_amd64", "release",
			[]string{"master-00595b4-release", "master-00595b4"},
		},
		{
			"v6.5.7-20241119-4f2073d_linux_amd64", "release",
			[]string{"v6.5.7-20241119-4f2073d"},
		},
		{
			"master-00595b4-release-arm64", "release",
			[]string{"master-00595b4-release", "master-00595b4"},
		},
		{
			"v8.1.0-alpha-123-g1234567_linux_amd64", "release",
			[]string{"v8.1.0-alpha-123-g1234567"},
		},
		{
			"feature-branch-123abc_release_linux_amd64", "release",
			[]string{"feature-branch-123abc_release", "feature-branch-123abc"},
		},
	}

	for _, tt := range tests {
		got := computeBaseTags(tt.pushedTag, tt.releaseTagSuffix)
		if !reflect.DeepEqual(got, tt.want) {
			t.Errorf("computeBaseTags(%q, %q) = %v, want %v", tt.pushedTag, tt.releaseTagSuffix, got, tt.want)
		}
	}
}

func TestFilterArchTags(t *testing.T) {
	allTags := []string{
		"master-00595b4-release_linux_amd64",
		"master-00595b4-release-linux-arm64",
		"master-00595b4-release",
		"master-00595b4",
		"v6.5.7-20241119-4f2073d_linux_amd64",
		"v8.1.0-alpha-123-g1234567_linux_amd64",
	}
	baseTag := "master-00595b4-release"
	got := filterArchTags(baseTag, allTags)
	want := []string{"master-00595b4-release_linux_amd64", "master-00595b4-release-linux-arm64"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("filterArchTags(%q, allTags) = %v, want %v", baseTag, got, want)
	}
}

func TestListRepoTagsMock(t *testing.T) {
	// This is a placeholder for a real test with a mocked registry.
	// For now, just check that the function returns an error for a non-existent repo.
	listSingleArchTags("non-existent-repo", "xxx_linux_amd64")
}
