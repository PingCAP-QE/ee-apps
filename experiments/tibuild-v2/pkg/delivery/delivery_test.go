package delivery_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

func TestLoadFileResolvesAnchorsAndMergeKeys(t *testing.T) {
	content := `
deprecated:
  - example.com/hub/old/image
image_definitions:
  sync_trunk: &sync_trunk
    description: delivery the trunk branch images.
    tags_regex:
      - ^(master|main)(-experiment)?$
    constant_tags: [nightly]
  sync_ga: &sync_ga
    description: delivery GA images.
    tags_regex:
      - ^v[0-9]+[.][0-9]+[.][0-9]+$
    tag_regex_replace: "$1"
image_copy_rules:
  example.com/hub/widget/image:
    - <<: *sync_trunk
      dest_repositories:
        - docker.io/pingcap/widget
    - <<: *sync_ga
      description: overridden description
      dest_repositories:
        - gcr.io/pingcap-public/widget
`
	path := filepath.Join(t.TempDir(), "delivery.yaml")
	require.NoError(t, os.WriteFile(path, []byte(content), 0o644))

	cfg, err := delivery.LoadFile(path)
	require.NoError(t, err)
	require.Equal(t, []string{"example.com/hub/old/image"}, cfg.Deprecated)

	rules := cfg.ImageCopyRules["example.com/hub/widget/image"]
	require.Len(t, rules, 2)
	require.Equal(t, []string{"^(master|main)(-experiment)?$"}, rules[0].TagsRegex)
	require.Equal(t, []string{"nightly"}, rules[0].ConstantTags)
	require.Equal(t, "delivery the trunk branch images.", rules[0].Description)
	require.Equal(t, []string{"docker.io/pingcap/widget"}, rules[0].DestRepositories)

	// The merge key must not win over an explicit field.
	require.Equal(t, "overridden description", rules[1].Description)
	require.Equal(t, "$1", rules[1].TagRegexReplace)
	require.Equal(t, []string{"gcr.io/pingcap-public/widget"}, rules[1].DestRepositories)
}

func TestLoadFileRejectsFilesWithoutImageRules(t *testing.T) {
	path := filepath.Join(t.TempDir(), "delivery.yaml")
	require.NoError(t, os.WriteFile(path, []byte("deprecated: []\n"), 0o644))

	_, err := delivery.LoadFile(path)
	require.Error(t, err)
}

func TestLoadFileMissingFile(t *testing.T) {
	_, err := delivery.LoadFile(filepath.Join(t.TempDir(), "absent.yaml"))
	require.Error(t, err)
}

func TestIsDeprecated(t *testing.T) {
	cfg := &delivery.Config{
		Deprecated: []string{"example.com/hub/pingcap/old/image"},
	}

	require.True(t, cfg.IsDeprecated("example.com/hub/pingcap/old/image"))
	require.True(t, cfg.IsDeprecated("example.com/hub/pingcap/old/image/sub"))
	// Prefix must break on a path boundary.
	require.False(t, cfg.IsDeprecated("example.com/hub/pingcap/old/image2"))
	require.False(t, cfg.IsDeprecated("example.com/hub/pingcap/other/image"))
}

func TestParseImageRef(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    delivery.ImageRef
		wantErr string
	}{
		{
			name:  "registry with port and tag",
			input: "localhost:5000/namespace/widget:v1.2.3",
			want:  delivery.ImageRef{Repository: "localhost:5000/namespace/widget", Tag: "v1.2.3"},
		},
		{
			name:  "armored digest is preserved",
			input: "us-docker.pkg.dev/project/hub/pingcap/widget/image:v8.5.0@sha256:" + sha256Fixture[:64],
			want: delivery.ImageRef{
				Repository: "us-docker.pkg.dev/project/hub/pingcap/widget/image",
				Tag:        "v8.5.0",
				Digest:     "sha256:" + sha256Fixture[:64],
			},
		},
		{
			name:    "digest without tag",
			input:   "us-docker.pkg.dev/project/hub/pingcap/widget/image@sha256:" + sha256Fixture[:64],
			wantErr: "must include a tag",
		},
		{
			name:    "missing tag",
			input:   "docker.io/pingcap/widget",
			wantErr: "must include a tag",
		},
		{
			name:    "empty reference",
			input:   "  ",
			wantErr: "empty image reference",
		},
		{
			name:    "empty tag",
			input:   "docker.io/pingcap/widget:",
			wantErr: "must include a tag",
		},
		{
			name:    "invalid digest",
			input:   "docker.io/pingcap/widget:v1.0.0@sha256:short",
			wantErr: "invalid digest",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := delivery.ParseImageRef(tt.input)
			if tt.wantErr != "" {
				require.ErrorContains(t, err, tt.wantErr)
				return
			}
			require.NoError(t, err)
			require.Equal(t, tt.want, got)
		})
	}
}

const sha256Fixture = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
