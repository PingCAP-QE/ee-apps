package registry

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync/atomic"
)

var identifier = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]*$`)
var objectID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$`)

type Config struct {
	Version string `json:"version"`
	Routes  []Rule `json:"routes"`
}

type Rule struct {
	ID       string  `json:"id"`
	Host     string  `json:"host"`
	Route    string  `json:"route"`
	Method   string  `json:"method"`
	Path     string  `json:"path"`
	Priority int     `json:"priority"`
	Mode     string  `json:"mode"`
	Checks   []Check `json:"checks"`
	segments []segment
}

type Check struct {
	Object     string `json:"object,omitempty"`
	Relation   string `json:"relation"`
	ObjectFrom string `json:"object_from,omitempty"`
	IDFrom     string `json:"id_from,omitempty"`
}

type segment struct {
	name    string
	literal string
}

type Request struct {
	Host         string
	Route        string
	Method       string
	ExternalPath string
	UpstreamPath string
}

type Match struct {
	Rule       Rule
	PathValues map[string]string
	MatchedOn  string
}

// Parse validates a complete registry snapshot. Unknown JSON fields are
// rejected so misspelled security fields cannot silently weaken a rule.
func Parse(data []byte) (*Registry, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var cfg Config
	if err := dec.Decode(&cfg); err != nil {
		return nil, fmt.Errorf("decode registry: %w", err)
	}
	if err := dec.Decode(new(any)); !errors.Is(err, io.EOF) {
		return nil, errors.New("registry must contain exactly one JSON value")
	}
	if strings.TrimSpace(cfg.Version) == "" {
		return nil, errors.New("registry version is required")
	}
	if len(cfg.Routes) == 0 {
		return nil, errors.New("registry must contain at least one route")
	}
	seenIDs := map[string]struct{}{}
	staticObjects := map[string]string{}
	for i := range cfg.Routes {
		r := &cfg.Routes[i]
		if err := validateRule(r); err != nil {
			return nil, fmt.Errorf("route[%d] %q: %w", i, r.ID, err)
		}
		if _, ok := seenIDs[r.ID]; ok {
			return nil, fmt.Errorf("duplicate route id %q", r.ID)
		}
		seenIDs[r.ID] = struct{}{}
		for _, check := range r.Checks {
			if check.Object == "" {
				continue
			}
			if prior, ok := staticObjects[check.Object]; ok && prior != check.Relation {
				return nil, fmt.Errorf("object naming conflict across registry: %s is checked with both %s and %s", check.Object, prior, check.Relation)
			}
			staticObjects[check.Object] = check.Relation
		}
	}
	if err := validateConflicts(cfg.Routes); err != nil {
		return nil, err
	}
	sort.SliceStable(cfg.Routes, func(i, j int) bool {
		if cfg.Routes[i].Priority != cfg.Routes[j].Priority {
			return cfg.Routes[i].Priority > cfg.Routes[j].Priority
		}
		return specificity(cfg.Routes[i].segments) > specificity(cfg.Routes[j].segments)
	})
	return &Registry{version: cfg.Version, rules: cfg.Routes}, nil
}

type Registry struct {
	version string
	rules   []Rule
}

func (r *Registry) Version() string { return r.version }

// Rules returns a defensive copy for reporting and inspection.
func (r *Registry) Rules() []Rule {
	rules := make([]Rule, len(r.rules))
	copy(rules, r.rules)
	for i := range rules {
		rules[i].Checks = append([]Check(nil), rules[i].Checks...)
		rules[i].segments = append([]segment(nil), rules[i].segments...)
	}
	return rules
}

// Match performs exact host, route, method and normalized path matching.
// External and upstream paths are both considered explicitly; no fallback is
// attempted for an unknown route or path.
func (r *Registry) Match(req Request) (Match, bool) {
	host := normalizeHost(req.Host)
	method := strings.ToUpper(strings.TrimSpace(req.Method))
	for _, rule := range r.rules {
		if rule.Host != host || rule.Route != req.Route || rule.Method != method {
			continue
		}
		for _, candidate := range []struct{ name, value string }{
			{"external", req.ExternalPath}, {"upstream", req.UpstreamPath},
		} {
			if candidate.value == "" {
				continue
			}
			values, ok := matchPath(rule.segments, candidate.value)
			if ok {
				return Match{Rule: rule, PathValues: values, MatchedOn: candidate.name}, true
			}
		}
	}
	return Match{}, false
}

func validateRule(r *Rule) error {
	if !identifier.MatchString(r.ID) || !identifier.MatchString(r.Route) {
		return errors.New("id and route must be stable identifiers")
	}
	r.Host = normalizeHost(r.Host)
	if r.Host == "" || strings.ContainsAny(r.Host, "/ \t\n") {
		return errors.New("host must be a hostname without a scheme or path")
	}
	r.Method = strings.ToUpper(strings.TrimSpace(r.Method))
	if r.Method == "" || strings.ContainsAny(r.Method, " /\t\n") {
		return errors.New("method must be an exact HTTP method")
	}
	if r.Mode != "off" && r.Mode != "observe" && r.Mode != "shadow" && r.Mode != "enforce" {
		return fmt.Errorf("unsupported mode %q", r.Mode)
	}
	var err error
	r.Path, r.segments, err = parseTemplate(r.Path)
	if err != nil {
		return err
	}
	if len(r.Checks) == 0 {
		return errors.New("at least one explicit authorization check is required")
	}
	pathParameters := map[string]struct{}{}
	for _, part := range r.segments {
		if part.name != "" {
			pathParameters[part.name] = struct{}{}
		}
	}
	objects := map[string]string{}
	for _, check := range r.Checks {
		if !identifier.MatchString(check.Relation) {
			return fmt.Errorf("invalid relation %q", check.Relation)
		}
		static, dynamic := check.Object != "", check.ObjectFrom != "" || check.IDFrom != ""
		if static == dynamic || (dynamic && (check.ObjectFrom == "" || check.IDFrom == "")) {
			return errors.New("check must define exactly one of object or object_from plus id_from")
		}
		if static {
			if err := validateObject(check.Object); err != nil {
				return err
			}
			if prior, ok := objects[check.Object]; ok && prior != check.Relation {
				return fmt.Errorf("object naming conflict: %s is checked with both %s and %s", check.Object, prior, check.Relation)
			}
			objects[check.Object] = check.Relation
		} else if !identifier.MatchString(check.ObjectFrom) || !validSource(check.IDFrom) {
			return fmt.Errorf("invalid dynamic object source %q from %q", check.ObjectFrom, check.IDFrom)
		} else if strings.HasPrefix(check.IDFrom, "path.") {
			parameter := strings.TrimPrefix(check.IDFrom, "path.")
			if _, ok := pathParameters[parameter]; !ok {
				return fmt.Errorf("path resource source %q is not present in path template %q", check.IDFrom, r.Path)
			}
		}
	}
	return nil
}

func validateObject(object string) error {
	parts := strings.SplitN(object, ":", 2)
	if len(parts) != 2 || !identifier.MatchString(parts[0]) || !objectID.MatchString(parts[1]) {
		return fmt.Errorf("invalid object name %q; expected type:id", object)
	}
	return nil
}

func validSource(source string) bool {
	if strings.HasPrefix(source, "path.") || strings.HasPrefix(source, "query.") || strings.HasPrefix(source, "body.") {
		return identifier.MatchString(strings.TrimPrefix(strings.TrimPrefix(strings.TrimPrefix(source, "path."), "query."), "body."))
	}
	return false
}

func parseTemplate(raw string) (string, []segment, error) {
	if raw == "" || raw[0] != '/' || strings.ContainsAny(raw, "?#%\\") {
		return "", nil, fmt.Errorf("path template must be absolute and contain no query, fragment, escape, or backslash: %q", raw)
	}
	if raw != "/" && (path.Clean(raw) != raw || strings.HasSuffix(raw, "/")) {
		return "", nil, fmt.Errorf("path template is not normalized: %q", raw)
	}
	if raw == "/" {
		return raw, []segment{}, nil
	}
	parts := strings.Split(strings.TrimPrefix(raw, "/"), "/")
	segments := make([]segment, 0, len(parts))
	for _, part := range parts {
		if strings.HasPrefix(part, "{") && strings.HasSuffix(part, "}") {
			name := strings.TrimSuffix(strings.TrimPrefix(part, "{"), "}")
			if !identifier.MatchString(name) {
				return "", nil, fmt.Errorf("invalid path parameter %q", part)
			}
			for _, prior := range segments {
				if prior.name == name {
					return "", nil, fmt.Errorf("duplicate path parameter %q", name)
				}
			}
			segments = append(segments, segment{name: name})
		} else {
			if part == "" || strings.ContainsAny(part, "{}") {
				return "", nil, fmt.Errorf("invalid path segment %q", part)
			}
			segments = append(segments, segment{literal: part})
		}
	}
	return raw, segments, nil
}

func validateConflicts(rules []Rule) error {
	for i := range rules {
		for j := i + 1; j < len(rules); j++ {
			a, b := rules[i], rules[j]
			if a.Host != b.Host || a.Route != b.Route || a.Method != b.Method || !templatesOverlap(a.segments, b.segments) {
				continue
			}
			if a.Priority == b.Priority && specificity(a.segments) == specificity(b.segments) {
				return fmt.Errorf("ambiguous route conflict between %q (%s) and %q (%s)", a.ID, a.Path, b.ID, b.Path)
			}
		}
	}
	return nil
}

func templatesOverlap(a, b []segment) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].name == "" && b[i].name == "" && a[i].literal != b[i].literal {
			return false
		}
	}
	return true
}

func specificity(parts []segment) int {
	n := 0
	for _, part := range parts {
		if part.name == "" {
			n++
		}
	}
	return n
}

func matchPath(template []segment, raw string) (map[string]string, bool) {
	if raw == "" || raw[0] != '/' || strings.ContainsAny(raw, "?#%\\") || path.Clean(raw) != raw || (raw != "/" && strings.HasSuffix(raw, "/")) {
		return nil, false
	}
	var parts []string
	if raw != "/" {
		parts = strings.Split(strings.TrimPrefix(raw, "/"), "/")
	}
	if len(parts) != len(template) {
		return nil, false
	}
	values := make(map[string]string)
	for i, expected := range template {
		if expected.name != "" {
			if parts[i] == "" || parts[i] == "." || parts[i] == ".." {
				return nil, false
			}
			values[expected.name] = parts[i]
		} else if expected.literal != parts[i] {
			return nil, false
		}
	}
	return values, true
}

func normalizeHost(host string) string {
	host = strings.ToLower(strings.TrimSpace(host))
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	return strings.TrimSuffix(host, ".")
}

// Store publishes only fully validated immutable snapshots.
type Store struct{ current atomic.Pointer[Registry] }

func (s *Store) Load() *Registry { return s.current.Load() }

func (s *Store) Replace(data []byte) error {
	next, err := Parse(data)
	if err != nil {
		return err
	}
	s.current.Store(next)
	return nil
}
