package delivery

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/dlclark/regexp2"
)

// TargetPlan is one destination repository a source image is copied to.
//
// Target is the registry host of Dest. It is the unit workers are sharded on:
// a worker only claims targets it has credentials and network access for.
type TargetPlan struct {
	Target string   `yaml:"target" json:"target"`
	Dest   string   `yaml:"dest" json:"dest"`
	Tags   []string `yaml:"tags" json:"tags"`
}

// ImageRef is a parsed image reference.
type ImageRef struct {
	Repository string
	Tag        string
	// Digest is only set when the caller pinned the reference.
	Digest string
}

var (
	// ErrEmptyReference is returned for a blank image reference.
	ErrEmptyReference = errors.New("empty image reference")
	// ErrMissingTag is returned when the reference carries no tag. The rules
	// match on tags, so a digest-only reference cannot be planned.
	ErrMissingTag = errors.New("image reference must include a tag")
	// ErrInvalidDigest is returned for a malformed digest suffix.
	ErrInvalidDigest = errors.New("invalid digest")
)

// ParseImageRef splits an image reference into repository, tag and optional
// digest. It mirrors the legacy generator: the tag separator is the last colon
// that follows the last slash, so registry ports (localhost:5000/...) are
// handled.
func ParseImageRef(ref string) (ImageRef, error) {
	ref = strings.TrimSpace(ref)
	if ref == "" {
		return ImageRef{}, ErrEmptyReference
	}

	var digest string
	if idx := strings.LastIndex(ref, "@"); idx >= 0 {
		digest = ref[idx+1:]
		ref = ref[:idx]
		if !isDigest(digest) {
			return ImageRef{}, fmt.Errorf("%w: %q", ErrInvalidDigest, digest)
		}
	}

	lastSlash := strings.LastIndex(ref, "/")
	lastColon := strings.LastIndex(ref, ":")
	if lastColon <= lastSlash {
		return ImageRef{}, fmt.Errorf("%w: %q", ErrMissingTag, ref)
	}

	repo, tag := ref[:lastColon], ref[lastColon+1:]
	if repo == "" || tag == "" {
		return ImageRef{}, fmt.Errorf("%w: %q", ErrMissingTag, ref)
	}
	return ImageRef{Repository: repo, Tag: tag, Digest: digest}, nil
}

func isDigest(digest string) bool {
	const prefix = "sha256:"
	if !strings.HasPrefix(digest, prefix) {
		return false
	}
	hex := digest[len(prefix):]
	if len(hex) != 64 {
		return false
	}
	for _, r := range hex {
		if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
			return false
		}
	}
	return true
}

// MatchImage expands the rules of cfg for the given image reference.
//
// The returned plans keep the rule order of the rules file and the destination
// order inside each rule; every plan lists all tags to write on that
// destination (the source tag first, then constant tags, then the tag derived by
// tag_regex_replace). Identical (dest, tag) pairs are removed, but a destination
// reached by two different rules yields two plans — the planner deduplicates
// them into a single step later.
//
// An unknown repository, an image with no matching rule and a deprecated
// repository all yield no plans and no error.
func (c *Config) MatchImage(src string) ([]TargetPlan, error) {
	return MatchImage(c, src)
}

// MatchImage expands the rules of cfg for src. It is the package-level form of
// (*Config).MatchImage.
func MatchImage(cfg *Config, src string) ([]TargetPlan, error) {
	ref, err := ParseImageRef(src)
	if err != nil {
		return nil, err
	}
	if cfg == nil || cfg.IsDeprecated(ref.Repository) {
		return nil, nil
	}

	rules := cfg.ImageCopyRules[ref.Repository]
	if len(rules) == 0 {
		return nil, nil
	}

	var plans []TargetPlan
	for _, rule := range rules {
		matched, err := matchingPatterns(rule.TagsRegex, ref.Tag)
		if err != nil {
			return nil, err
		}
		if len(matched) == 0 {
			continue
		}

		derived := ""
		if rule.TagRegexReplace != "" {
			derived, err = replaceFirst(matched[0], rule.TagRegexReplace, ref.Tag)
			if err != nil {
				return nil, err
			}
		}

		for _, dest := range rule.DestRepositories {
			tags := make([]string, 0, len(rule.ConstantTags)+2)
			tags = append(tags, ref.Tag)
			tags = append(tags, rule.ConstantTags...)
			if derived != "" && derived != ref.Tag {
				tags = append(tags, derived)
			}
			plans = append(plans, TargetPlan{
				Target: TargetOf(dest),
				Dest:   dest,
				Tags:   dedupe(tags),
			})
		}
	}
	return plans, nil
}

// TargetOf returns the registry host of a destination repository. The first
// path segment is a host when it looks like one (a dot, a port or localhost);
// otherwise the reference relies on the default registry.
func TargetOf(dest string) string {
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

// matchingPatterns returns the patterns of the rule that match tag, in rule
// order.
func matchingPatterns(patterns []string, tag string) ([]string, error) {
	var matched []string
	for _, pattern := range patterns {
		ok, err := matchPattern(pattern, tag)
		if err != nil {
			return nil, err
		}
		if ok {
			matched = append(matched, pattern)
		}
	}
	return matched, nil
}

const regexMatchTimeout = 2 * time.Second

var regexCache sync.Map // pattern -> *regexp2.Regexp

// compilePattern compiles an ECMAScript pattern.
//
// The rules use negative lookahead (`(?!pre)`) which RE2 (the standard regexp
// package) cannot express, and the legacy generator evaluated them with
// JavaScript's RegExp. regexp2 in ECMAScript mode reproduces that behaviour; the
// compile results are cached because every match would otherwise pay for the
// parse.
func compilePattern(pattern string) (*regexp2.Regexp, error) {
	if cached, ok := regexCache.Load(pattern); ok {
		return cached.(*regexp2.Regexp), nil
	}
	re, err := regexp2.Compile(pattern, regexp2.ECMAScript)
	if err != nil {
		return nil, fmt.Errorf("delivery: invalid regex %q: %w", pattern, err)
	}
	re.MatchTimeout = regexMatchTimeout
	regexCache.Store(pattern, re)
	return re, nil
}

func matchPattern(pattern, tag string) (bool, error) {
	re, err := compilePattern(pattern)
	if err != nil {
		return false, err
	}
	ok, err := re.MatchString(tag)
	if err != nil {
		return false, fmt.Errorf("delivery: match %q against %q: %w", tag, pattern, err)
	}
	return ok, nil
}

func replaceFirst(pattern, replacement, tag string) (string, error) {
	re, err := compilePattern(pattern)
	if err != nil {
		return "", err
	}
	out, err := re.Replace(tag, replacement, -1, 1)
	if err != nil {
		return "", fmt.Errorf("delivery: replace %q with %q in %q: %w", pattern, replacement, tag, err)
	}
	return out, nil
}

func dedupe(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	out := values[:0]
	for _, value := range values {
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		out = append(out, value)
	}
	return out
}
