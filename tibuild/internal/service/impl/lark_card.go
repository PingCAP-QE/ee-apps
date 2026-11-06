package impl

import (
	"html"
	"net/url"
	"path"
	"sort"
	"strings"

	"github.com/google/go-containerregistry/pkg/name"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/devbuild"
)

// escapeCardText also protects Lark's Markdown extensions (mentions, tags and
// links) when values originate in build results or user-provided git refs.
func escapeCardText(text string) string {
	text = html.EscapeString(text)
	return strings.NewReplacer("*", "&#42;", "_", "&#95;", "`", "&#96;", "[", "&#91;", "]", "&#93;", "~", "&#126;").Replace(text)
}

// inlineCardCode keeps artifact values literal, including underscores. A fence
// longer than any backtick run prevents malformed values from escaping the span.
func inlineCardCode(value string) string {
	value = strings.NewReplacer("\r", " ", "\n", " ").Replace(value)
	longest, run := 0, 0
	for _, r := range value {
		if r == '`' {
			run++
			if run > longest {
				longest = run
			}
		} else {
			run = 0
		}
	}
	fence := strings.Repeat("`", longest+1)
	return fence + " " + value + " " + fence
}

func cardLink(label, target string) string {
	if !validHTTPURL(target) {
		return escapeCardText(label)
	}
	u, _ := url.Parse(target)
	// Parentheses must not terminate Markdown links.
	target = strings.ReplaceAll(strings.ReplaceAll(u.String(), "(", "%28"), ")", "%29")
	return "[" + escapeCardText(label) + "](" + target + ")"
}

// imageSyncURL opens TCMS's confirmation dialog with the exact image reference.
func imageSyncURL(base, reference string) string {
	if !validHTTPURL(base) {
		return ""
	}
	if _, err := name.ParseReference(reference); err != nil {
		return ""
	}
	u, _ := url.Parse(base)
	query := u.Query()
	query.Set("sourceImage", reference)
	u.RawQuery = query.Encode()
	return u.String()
}

func validHTTPURL(raw string) bool {
	u, err := url.Parse(raw)
	return err == nil && u.Host != "" && (u.Scheme == "https" || u.Scheme == "http") && u.User == nil
}

type imageTag struct{ Tag, Reference string }
type imagePlatform struct {
	Platform string
	Tags     []imageTag
}
type imageRepository struct {
	Repo      string
	Platforms []imagePlatform
}

// splitImageReference preserves registry ports, digest references and the exact
// input text. An untagged reference is displayed intact rather than truncated.
func splitImageReference(ref string) (repo, tag string) {
	if i := strings.LastIndex(ref, "@"); i >= 0 {
		return ref[:i], ref[i:]
	}
	if i := strings.LastIndex(ref, ":"); i > strings.LastIndex(ref, "/") {
		return ref[:i], ref[i+1:]
	}
	return ref, ref
}

func artifactPlatform(platform, reference string) string {
	if platform != "" {
		return platform
	}
	// Older reports may omit platform metadata.
	for _, os := range []string{"linux", "darwin", "windows"} {
		for _, arch := range []string{"amd64", "arm64"} {
			if strings.Contains(reference, "_"+os+"_"+arch) || strings.Contains(reference, "-"+os+"-"+arch) {
				return os + "/" + arch
			}
		}
	}
	return ""
}

func groupNotificationImages(images []ImageInfo) []imageRepository {
	repos := map[string]map[string][]imageTag{}
	seen := map[string]bool{}
	for _, image := range images {
		if image.URL == "" || seen[image.URL] {
			continue
		}
		seen[image.URL] = true
		repo, tag := splitImageReference(image.URL)
		platform := artifactPlatform(image.Platform, tag)
		if platform == "" {
			platform = MultiArch
		}
		if repos[repo] == nil {
			repos[repo] = map[string][]imageTag{}
		}
		repos[repo][platform] = append(repos[repo][platform], imageTag{tag, image.URL})
	}
	var result []imageRepository
	for repo, platforms := range repos {
		group := imageRepository{Repo: repo}
		for platform, tags := range platforms {
			sort.Slice(tags, func(i, j int) bool { return tags[i].Tag < tags[j].Tag })
			group.Platforms = append(group.Platforms, imagePlatform{platform, tags})
		}
		sort.Slice(group.Platforms, func(i, j int) bool {
			a, b := group.Platforms[i].Platform, group.Platforms[j].Platform
			if a == MultiArch || b == MultiArch {
				return a == MultiArch && b != MultiArch
			}
			return a < b
		})
		result = append(result, group)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Repo < result[j].Repo })
	return result
}

func notificationDownloadURL(base, reference string) string {
	if !validHTTPURL(base) || reference == "" {
		return ""
	}
	// The tag delimiter is after the last repo slash; filenames may contain paths.
	colon := strings.LastIndex(reference, ":")
	if colon < 0 {
		return ""
	}
	repo, remainder := reference[:colon], reference[colon+1:]
	tag, file, ok := strings.Cut(remainder, "/")
	if !ok || tag == "" || file == "" {
		return ""
	}
	return buildArtifactURL(strings.TrimRight(base, "/"), &devbuild.OciFile{Repo: repo, Tag: tag, File: file})
}

// notificationCardData contains artifact data and links for the card template.
type notificationCardData struct {
	*NotificationInfo
	ImageGroups  []imageRepository
	BinaryGroups []notificationBinaryGroup
}

type notificationBinaryGroup struct {
	Platform string
	Files    []notificationBinary
}

type notificationBinary struct {
	FileName, URL, Sha256URL  string
	Downloadable, HasChecksum bool
}

func prepareNotificationCard(info *NotificationInfo) notificationCardData {
	data := notificationCardData{NotificationInfo: info, ImageGroups: groupNotificationImages(info.Images)}
	bins := append([]BinaryInfo(nil), info.Binaries...)
	sort.SliceStable(bins, func(i, j int) bool {
		a, b := artifactPlatform(bins[i].Platform, bins[i].OciReference), artifactPlatform(bins[j].Platform, bins[j].OciReference)
		if a != b {
			return a < b
		}
		af, bf := bins[i].FileName, bins[j].FileName
		if af == "" {
			af = path.Base(bins[i].OciReference)
		}
		if bf == "" {
			bf = path.Base(bins[j].OciReference)
		}
		if af != bf {
			return af < bf
		}
		return bins[i].OciReference < bins[j].OciReference
	})
	seen := map[string]bool{}
	for _, bin := range bins {
		if strings.HasSuffix(bin.OciReference, ".sha256") || seen[bin.OciReference] {
			continue
		}
		seen[bin.OciReference] = true
		platform := artifactPlatform(bin.Platform, bin.OciReference)
		if len(data.BinaryGroups) == 0 || data.BinaryGroups[len(data.BinaryGroups)-1].Platform != platform {
			data.BinaryGroups = append(data.BinaryGroups, notificationBinaryGroup{Platform: platform})
		}
		filename := bin.FileName
		if filename == "" {
			filename = path.Base(bin.OciReference)
		}
		file := notificationBinary{
			FileName: filename, URL: bin.URL, Sha256URL: bin.Sha256URL,
			Downloadable: validHTTPURL(bin.URL),
			HasChecksum:  bin.Sha256Reference != "" || bin.Sha256URL != "",
		}
		index := len(data.BinaryGroups) - 1
		data.BinaryGroups[index].Files = append(data.BinaryGroups[index].Files, file)
	}
	return data
}
