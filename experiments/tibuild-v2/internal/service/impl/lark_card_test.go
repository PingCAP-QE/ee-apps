package impl

import (
	"net/url"
	"strings"
	"testing"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/ent"
	"github.com/PingCAP-QE/ee-apps/tibuild/internal/database/schema"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/stretchr/testify/require"
	tknv1 "github.com/tektoncd/pipeline/pkg/apis/pipeline/v1"
)

func TestNotificationImageGrouping(t *testing.T) {
	images := []ImageInfo{
		{Platform: "linux/arm64", URL: "registry:5000/a:v1_linux_arm64"},
		{Platform: "linux/amd64", URL: "registry:5000/b:v1_linux_amd64"},
		{Platform: "linux/amd64", URL: "registry:5000/a:v2_linux_amd64"},
		{Platform: MultiArch, URL: "registry:5000/a:v1"},
		{Platform: "linux/amd64", URL: "registry:5000/a:v1_linux_amd64"},
		{Platform: "linux/amd64", URL: "registry:5000/a:v1_linux_amd64"},
	}
	groups := groupNotificationImages(images)
	require.Len(t, groups, 2)
	require.Equal(t, "registry:5000/a", groups[0].Repo)
	require.Len(t, groups[0].Platforms, 3)
	require.Equal(t, MultiArch, groups[0].Platforms[0].Platform)
	require.Equal(t, "linux/amd64", groups[0].Platforms[1].Platform)
	require.Len(t, groups[0].Platforms[1].Tags, 2)
	require.Equal(t, "v1_linux_amd64", groups[0].Platforms[1].Tags[0].Tag)
	require.Len(t, groups[1].Platforms[0].Tags, 1)
	// Sorting must be independent of Tekton callback arrival order.
	for i, j := 0, len(images)-1; i < j; i, j = i+1, j-1 {
		images[i], images[j] = images[j], images[i]
	}
	require.Equal(t, groups, groupNotificationImages(images))
	repo, tag := splitImageReference("registry:5000/a@sha256:abcdef")
	require.Equal(t, "registry:5000/a", repo)
	require.Equal(t, "@sha256:abcdef", tag)
}

func TestNotificationBinaryLinksAndChecksums(t *testing.T) {
	info := buildNotificationInfo(&ent.DevBuild{Status: "SUCCESS", BuildReport: schema.BuildReport{Binaries: []schema.OciArtifact{
		{Repo: "registry:5000/a/package", Tag: "v1_linux_arm64", Platform: "linux/arm64", Files: []string{"b.tar.gz.sha256", "b.tar.gz"}},
		{Repo: "registry:5000/a/package", Tag: "v1_linux_amd64", Platform: "linux/amd64", Files: []string{"a.tar.gz", "a.tar.gz.sha256", "no-checksum.tar.gz"}},
	}}})
	require.Len(t, info.Binaries, 3)
	require.Equal(t, "registry:5000/a/package:v1_linux_arm64/b.tar.gz.sha256", info.Binaries[0].Sha256Reference)
	for i := range info.Binaries {
		bin := &info.Binaries[i]
		bin.URL = notificationDownloadURL("https://downloads.example.com/oci-file", bin.OciReference)
		bin.Sha256URL = notificationDownloadURL("https://downloads.example.com/oci-file", bin.Sha256Reference)
	}
	u, err := url.Parse(info.Binaries[0].URL)
	require.NoError(t, err)
	require.Equal(t, "b.tar.gz", u.Query().Get("file"))
	require.Equal(t, "v1_linux_arm64", u.Query().Get("tag"))
	card := renderLarkCard(t, info)
	var markdown []string
	walkCard(card, func(el map[string]any) {
		if el["tag"] == "markdown" {
			markdown = append(markdown, el["content"].(string))
		}

	})
	text := strings.Join(markdown, "\n")
	require.Less(t, strings.Index(text, "**linux/amd64:**"), strings.Index(text, "**linux/arm64:**"))
	require.Less(t, strings.Index(text, "a.tar.gz]"), strings.Index(text, "no-checksum.tar.gz]"))
	require.Contains(t, text, "[sha256](https://")
	for _, bin := range info.Binaries {
		require.Contains(t, text, bin.URL)
		if bin.Sha256URL != "" {
			require.Contains(t, text, bin.Sha256URL)
		}
	}
	require.NotContains(t, text, "```")
}

func walkCard(value any, visit func(map[string]any)) {
	switch v := value.(type) {
	case map[string]any:
		visit(v)
		for _, child := range v {
			walkCard(child, visit)
		}
	case []any:
		for _, child := range v {
			walkCard(child, visit)
		}
	}
}

func TestNotificationReferencesWithoutCallbacks(t *testing.T) {
	info := &NotificationInfo{Status: "SUCCESS", Images: []ImageInfo{
		{Platform: MultiArch, URL: "registry/a:v1"},
		{Platform: "linux/amd64", URL: "registry/a:v1_linux_amd64"},
		{Platform: "linux/amd64", URL: "registry/a:v2_linux_amd64"},
		{Platform: MultiArch, URL: "registry/b:v1"},
	}}
	card := renderLarkCard(t, info)
	var markdown []string
	walkCard(card, func(el map[string]any) {
		require.NotEqual(t, "button", el["tag"])
		require.NotContains(t, el, "behaviors")
		if el["tag"] == "markdown" {
			markdown = append(markdown, el["content"].(string))
		}
	})
	text := strings.Join(markdown, "\n")
	for _, image := range info.Images {
		repo, tag := splitImageReference(image.URL)
		require.Contains(t, text, inlineCardCode(repo))
		require.Contains(t, text, inlineCardCode(tag))
		require.NotContains(t, text, "]("+image.URL+")")
	}
	require.NotContains(t, text, "https://registry/")
	require.Contains(t, text, "**Multi-arch:**")
}

func TestMultipleMultiArchTagsArePreserved(t *testing.T) {
	results := []tknv1.PipelineRunResult{{Name: "pushed-images", Value: *tknv1.NewStructuredValues(`images:
- repo: registry/a
  tag: v1_linux_amd64
  multi_arch_tags: [v1, latest]
- repo: registry/b
  tag: v1_linux_amd64
  multi_arch_tags: [v1, latest]
`)}}
	_, images := extractArtifactsFromResults(results, nil)
	require.Len(t, images, 6)
	pr := tknv1.PipelineRun{}
	pr.Status.Results = results
	report := buildBuildReport([]tknv1.PipelineRun{pr, pr})
	require.Len(t, report.Images, 6)
	for _, repo := range []string{"registry/a", "registry/b"} {
		for _, tag := range []string{"v1", "latest"} {
			require.Contains(t, report.Images, schema.ImageArtifact{Platform: MultiArch, URL: repo + ":" + tag})
		}
	}
}

func renderLarkCard(t testing.TB, info *NotificationInfo) map[string]any {
	t.Helper()
	card, err := NewLarkCardWithGoTemplate(info)
	require.NoError(t, err)
	return card
}

func TestLarkEnglishText(t *testing.T) {
	info := &NotificationInfo{Status: "SUCCESS", Product: "tiflash", CreatedBy: "name\"with\nnewline", IsHotfix: true, GitRef: "tag/v1", GithubRepo: "pingcap/tiflash", ErrMsg: "unsafe <at id=all> **text**", Images: []ImageInfo{{Platform: MultiArch, URL: "registry/a:v1"}}, Binaries: []BinaryInfo{{OciReference: "registry/a:v1/a.tar.gz", FileName: "a.tar.gz", Sha256Reference: "registry/a:v1/a.tar.gz.sha256"}}, PipelineRuns: []PipelineRunInfo{{Name: "test", Status: "Succeeded"}}}
	card := renderLarkCard(t, info)
	require.Equal(t, "2.0", card["schema"])
	require.NotContains(t, card, "i18n_elements")
	require.NotContains(t, card, "i18n_body")
	var contents []string
	walkCard(card, func(el map[string]any) {
		require.NotContains(t, el, "i18n_content")
		if content, ok := el["content"].(string); ok {
			contents = append(contents, content)
		}
	})
	text := strings.Join(contents, "\n")
	for _, expected := range []string{"**Multi-arch:**", "**Docker Images:**", "**Binaries:**", "sha256", "**Created By:**", "**Hotfix:**", "**Pipeline Runs:**", "**Error:**"} {
		require.Contains(t, text, expected)
	}
	require.NotContains(t, text, "<at id=all>")
}

func TestCompactCardKeepsOriginalSections(t *testing.T) {
	info := &NotificationInfo{Status: "SUCCESS", CreatedBy: "ci-bot", CreatedAt: "2026-10-15T01:00:00Z", CompletedAt: "2026-10-15T01:10:00Z", Product: "tiflash", Edition: "nextgen", Version: "v1", Platform: "linux", GitRef: "tag/v1", GithubRepo: "pingcap/tiflash", GitSha: "abcdef", Images: []ImageInfo{
		{Platform: MultiArch, URL: "registry/a:v1"},
		{Platform: MultiArch, URL: "registry/a:v2"},
		{Platform: "linux/amd64", URL: "registry/a:v1_linux_amd64"},
	}, Binaries: []BinaryInfo{{Platform: "linux/amd64", FileName: "a.tar.gz", OciReference: "registry/a:v1/a.tar.gz", URL: "https://download.example/a.tar.gz", Sha256URL: "https://download.example/a.tar.gz.sha256"}, {Platform: "linux/amd64", FileName: "b.tar.gz", OciReference: "registry/a:v1/b.tar.gz", URL: "https://download.example/b.tar.gz"}}, PipelineRuns: []PipelineRunInfo{{Name: "build", Status: "Succeeded", URL: "https://tekton.example/run"}}}
	card := renderLarkCard(t, info)
	elements := card["body"].(map[string]any)["elements"].([]any)
	status := elements[0].(map[string]any)
	require.Equal(t, "div", status["tag"])
	fields := status["fields"].([]any)
	require.Len(t, fields, 4)
	expected := []string{"**Status:**\t✅SUCCESS", "🧑‍💻 **Created By:**\tci-bot", "🛫 **Created at:**\t2026-10-15T01:00:00Z", "🛬 **Completed time:**\t2026-10-15T01:10:00Z"}
	for i, f := range fields {
		field := f.(map[string]any)
		require.Equal(t, false, field["is_short"])
		require.Equal(t, expected[i], field["text"].(map[string]any)["content"])
	}
	require.Equal(t, "hr", elements[1].(map[string]any)["tag"])
	require.Equal(t, "**Request:**\n- **Product:** tiflash\n- **Edition:** nextgen\n- **Version:** v1\n- **Platform:** linux\n- **Repository:** [pingcap/tiflash](https://github.com/pingcap/tiflash)\n- **Git Ref:** [tag/v1](https://github.com/pingcap/tiflash/tree/v1)", elements[2].(map[string]any)["content"])
	require.Equal(t, "hr", elements[3].(map[string]any)["tag"])
	report := elements[4].(map[string]any)["content"].(string)
	require.Contains(t, report, "\n- **Docker Images:**\n    - ` registry/a `\n        - **Multi-arch:** ` v1 ` · ` v2 `\n        - **linux/amd64:** ` v1_linux_amd64 `")
	assertBinaryRows(t, card, 2)
	var rules int
	walkCard(card, func(el map[string]any) {
		require.NotContains(t, el, "background_style")
		if size, ok := el["text_size"]; ok {
			require.Equal(t, "notation", size)
			require.Contains(t, el["content"], "[sha256](")
		}
		if el["tag"] == "hr" {
			rules++
		}
	})
	require.Equal(t, 3, rules)
}

func TestBuildReportItems(t *testing.T) {
	info := &NotificationInfo{Status: "SUCCESS", Images: []ImageInfo{{Platform: MultiArch, URL: "registry/a:v1"}, {Platform: "linux/amd64", URL: "registry/a:v1_linux_amd64"}, {Platform: MultiArch, URL: "registry/b:v2"}}, Binaries: []BinaryInfo{{Platform: "linux/amd64", FileName: "a.tar.gz", OciReference: "registry/a:v1/a.tar.gz", URL: "https://download.example/a.tar.gz", Sha256URL: "https://download.example/a.tar.gz.sha256"}}}
	card := renderLarkCard(t, info)
	var report string
	walkCard(card, func(el map[string]any) {
		if el["tag"] == "markdown" && strings.HasPrefix(el["content"].(string), "**Build Report**") {
			report = el["content"].(string)
		}
	})
	require.NotEmpty(t, report)
	require.Contains(t, report, "\n- **Docker Images:**\n    - ` registry/a `")
	require.Contains(t, report, "\n    - ` registry/b `\n        - **Multi-arch:** ` v2 `")
	require.Contains(t, report, "\n        - **linux/amd64:** ` v1_linux_amd64 `")
	assertBinaryRows(t, card, 1)
	require.NotContains(t, report, "](registry/")
}

func TestInlineArtifactCode(t *testing.T) {
	require.Equal(t, "` registry:5000/team/image `", inlineCardCode("registry:5000/team/image"))
	require.Equal(t, "` v1_linux_amd64 `", inlineCardCode("v1_linux_amd64"))
	require.Equal(t, "`` tag`value ``", inlineCardCode("tag`value"))
	require.Equal(t, "` first second `", inlineCardCode("first\nsecond"))
}

func TestImageSyncShortcuts(t *testing.T) {
	images := []ImageInfo{
		{URL: "us-docker.pkg.dev/pingcap-testing-account/dev/pingcap/tiflash/image:v1"},
		{URL: "us-docker.pkg.dev/pingcap-testing-account/dev/pingcap/tiflash/image:v2"},
		{Platform: "linux/amd64", URL: "registry:5000/team/image:v1_linux_amd64"},
		{URL: "registry/b:v3"},
	}
	for _, hotfix := range []bool{false, true} {
		card := renderLarkCard(t, &NotificationInfo{Status: "SUCCESS", IsHotfix: hotfix, Images: images, ImageSyncURL: "https://sync.example.com/custom/form"})
		var report string
		walkCard(card, func(el map[string]any) {
			if el["tag"] == "markdown" && strings.HasPrefix(el["content"].(string), "**Build Report**") {
				report = el["content"].(string)
			}
		})
		if hotfix {
			require.NotContains(t, report, "[🧪 Sync]")
			require.NotContains(t, report, "sync.example.com")
			continue
		}
		require.Equal(t, len(images), strings.Count(report, "[🧪 Sync]("))
		for _, image := range images {
			link := imageSyncURL("https://sync.example.com/custom/form", image.URL)
			u, err := url.Parse(link)
			require.NoError(t, err)
			require.Equal(t, "https", u.Scheme)
			require.Equal(t, "sync.example.com", u.Host)
			require.Equal(t, "/custom/form", u.Path)
			require.Equal(t, image.URL, u.Query().Get("sourceImage"))
			require.Contains(t, report, cardLink("🧪 Sync", link))
		}
	}
	require.Empty(t, imageSyncURL("https://sync.example.com/form", "not an image"))
}

func TestImageSyncURLConfiguration(t *testing.T) {
	for _, base := range []string{"", "/relative", "javascript:alert(1)", "https://user:password@example.com/form"} {
		require.Empty(t, imageSyncURL(base, "registry/a:v1"))
	}
	link := imageSyncURL("https://sync.example.com/form?target=ecr&sourceImage=old#section", "registry:5000/team/image:v1")
	u, err := url.Parse(link)
	require.NoError(t, err)
	require.Equal(t, "ecr", u.Query().Get("target"))
	require.Equal(t, "registry:5000/team/image:v1", u.Query().Get("sourceImage"))
	require.Equal(t, "section", u.Fragment)

	card := renderLarkCard(t, &NotificationInfo{Status: "SUCCESS", Images: []ImageInfo{{URL: "registry/a:v1"}}})
	walkCard(card, func(el map[string]any) {
		if text, ok := el["content"].(string); ok {
			require.NotContains(t, text, "[🧪 Sync]")
		}
	})

	n := &LarkNotifier{}
	cfg := &config.Service{}
	cfg.Lark.ImageSyncURL = "https://sync.example.com/form"
	n.configureArtifacts(cfg)
	require.Equal(t, cfg.Lark.ImageSyncURL, n.options.Load().(notificationOptions).ImageSyncURL)
	cfg.Lark.ImageSyncURL = ""
	n.configureArtifacts(cfg)
	require.Empty(t, n.options.Load().(notificationOptions).ImageSyncURL)
}

func assertBinaryRows(t *testing.T, card map[string]any, count int) {
	t.Helper()
	var rows []map[string]any
	walkCard(card, func(el map[string]any) {
		if el["tag"] == "column_set" && el["margin"] == "0px 0px 0px 32px" {
			rows = append(rows, el)
		}
	})
	require.Len(t, rows, count)
	for _, row := range rows {
		require.Equal(t, "none", row["flex_mode"])
		require.Equal(t, "0px 0px 0px 32px", row["margin"])
		columns := row["columns"].([]any)
		fileColumn := columns[0].(map[string]any)
		require.Equal(t, "weighted", fileColumn["width"])
		file := fileColumn["elements"].([]any)[0].(map[string]any)
		require.NotContains(t, file, "text_size")
		require.Contains(t, file["content"], ".tar.gz](https://download.example/")
		if strings.Contains(file["content"].(string), "b.tar.gz]") {
			require.Len(t, columns, 1)
			continue
		}
		require.Len(t, columns, 2)
		checksumColumn := columns[1].(map[string]any)
		require.Equal(t, "auto", checksumColumn["width"])
		checksum := checksumColumn["elements"].([]any)[0].(map[string]any)
		require.Equal(t, "notation", checksum["text_size"])
		require.Equal(t, "[sha256](https://download.example/a.tar.gz.sha256)", checksum["content"])
	}
}
