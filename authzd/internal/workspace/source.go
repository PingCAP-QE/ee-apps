// Package workspace reads group membership from the Google Workspace Directory
// API and turns it into relationship tuples.
//
// It is a relationships.Source: the reconciler decides what to do with the
// snapshot, including refusing one that is incomplete. This package only reads,
// and it fails closed on anything it cannot interpret.
package workspace

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/relationships"
)

const (
	defaultBaseURL       = "https://admin.googleapis.com"
	defaultSubjectPrefix = "user:google/sub/"
	defaultPageSize      = 200
	defaultMaxPages      = 50
)

// Config describes one group and the team object its members belong to.
type Config struct {
	// BaseURL overrides the Directory API endpoint. Empty uses production.
	BaseURL string
	// GroupKey is the group's email address or immutable group ID.
	GroupKey string
	// TeamObject is the OpenFGA object that receives the member relation,
	// e.g. "team:all-staff".
	TeamObject string
	// SubjectPrefix is prepended to the Directory member ID. The ID token `sub`
	// claim is expected to equal that ID; verify before the first real run.
	SubjectPrefix string
	// Token returns a bearer token for the Directory API. The caller owns
	// acquisition and refresh.
	Token func(context.Context) (string, error)
	// PageSize and MaxPages bound one read.
	PageSize int
	MaxPages int
	// HTTPClient overrides the client (tests).
	HTTPClient *http.Client
}

// Source reads a Workspace group as relationship tuples.
type Source struct {
	baseURL    string
	groupKey   string
	teamObject string
	prefix     string
	token      func(context.Context) (string, error)
	pageSize   int
	maxPages   int
	client     *http.Client
	now        func() time.Time
}

type member struct {
	ID     string `json:"id"`
	Email  string `json:"email"`
	Type   string `json:"type"`
	Status string `json:"status"`
}

type memberList struct {
	Members       []member `json:"members"`
	NextPageToken string   `json:"nextPageToken"`
}

// NewSource validates the configuration. A source that cannot name its group,
// its team object, or its credential is a configuration error, not an empty
// group.
func NewSource(cfg Config) (*Source, error) {
	if strings.TrimSpace(cfg.GroupKey) == "" {
		return nil, errors.New("workspace group key is required")
	}
	if strings.TrimSpace(cfg.TeamObject) == "" {
		return nil, errors.New("team object is required")
	}
	if objectType, _, found := strings.Cut(cfg.TeamObject, ":"); !found || objectType != "team" {
		return nil, errors.New("team object must look like team:<id>")
	}
	if cfg.Token == nil {
		return nil, errors.New("a credential source is required")
	}
	baseURL := strings.TrimSpace(cfg.BaseURL)
	if baseURL == "" {
		baseURL = defaultBaseURL
	}
	parsed, err := url.Parse(baseURL)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil {
		return nil, errors.New("workspace base URL must be an HTTP(S) endpoint without credentials")
	}
	prefix := cfg.SubjectPrefix
	if prefix == "" {
		prefix = defaultSubjectPrefix
	}
	if !strings.HasPrefix(prefix, "user:") {
		return nil, errors.New("subject prefix must start with user:")
	}
	pageSize := cfg.PageSize
	if pageSize <= 0 {
		pageSize = defaultPageSize
	}
	maxPages := cfg.MaxPages
	if maxPages <= 0 {
		maxPages = defaultMaxPages
	}
	client := cfg.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	return &Source{
		baseURL: strings.TrimSuffix(baseURL, "/"), groupKey: cfg.GroupKey, teamObject: cfg.TeamObject,
		prefix: prefix, token: cfg.Token, pageSize: pageSize, maxPages: maxPages,
		client: client, now: time.Now,
	}, nil
}

// WithClock overrides the timestamp recorded on the snapshot.
func (s *Source) WithClock(now func() time.Time) *Source {
	if now != nil {
		s.now = now
	}
	return s
}

// Fetch reads the whole group. Any failure returns an error and no snapshot:
// the reconciler must never see a partial member list, because it would prune
// the members it did not manage to read.
func (s *Source) Fetch(ctx context.Context) (relationships.Snapshot, error) {
	if s == nil {
		return relationships.Snapshot{}, errors.New("workspace source is not configured")
	}
	token, err := s.token(ctx)
	if err != nil {
		return relationships.Snapshot{}, fmt.Errorf("workspace credential: %w", err)
	}
	if strings.TrimSpace(token) == "" {
		return relationships.Snapshot{}, errors.New("workspace credential is empty")
	}

	var (
		tuples  []relationships.Tuple
		seen    = map[string]struct{}{}
		pageTok string
	)
	for page := 0; page < s.maxPages; page++ {
		list, err := s.listMembers(ctx, token, pageTok)
		if err != nil {
			return relationships.Snapshot{}, err
		}
		for _, m := range list.Members {
			if !strings.EqualFold(m.Type, "USER") || strings.EqualFold(m.Status, "ARCHIVED") {
				continue
			}
			if !validMemberID(m.ID) {
				return relationships.Snapshot{}, fmt.Errorf("workspace member has a non-numeric id %q", m.ID)
			}
			key := s.prefix + m.ID
			if _, dup := seen[key]; dup {
				continue
			}
			seen[key] = struct{}{}
			tuples = append(tuples, relationships.Tuple{User: key, Relation: "member", Object: s.teamObject})
		}
		pageTok = list.NextPageToken
		if pageTok == "" {
			sort.Slice(tuples, func(i, j int) bool { return tuples[i].Key() < tuples[j].Key() })
			return relationships.Snapshot{
				Source:      "workspace-group/" + s.groupKey,
				Revision:    digest(tuples),
				GeneratedAt: s.now().UTC(),
				Complete:    true,
				Tuples:      tuples,
			}, nil
		}
	}
	return relationships.Snapshot{}, fmt.Errorf("workspace group pagination exceeded %d pages", s.maxPages)
}

func (s *Source) listMembers(ctx context.Context, token, pageToken string) (memberList, error) {
	endpoint, err := url.Parse(s.baseURL + "/admin/directory/v1/groups/" + url.PathEscape(s.groupKey) + "/members")
	if err != nil {
		return memberList{}, fmt.Errorf("build directory URL: %w", err)
	}
	query := endpoint.Query()
	query.Set("maxResults", strconv.Itoa(s.pageSize))
	query.Set("roles", "MEMBER")
	if pageToken != "" {
		query.Set("pageToken", pageToken)
	}
	endpoint.RawQuery = query.Encode()

	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint.String(), nil)
	if err != nil {
		return memberList{}, fmt.Errorf("build directory request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	response, err := s.client.Do(request)
	if err != nil {
		return memberList{}, fmt.Errorf("list group members: %w", err)
	}
	defer func() { _ = response.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(response.Body, 8<<20))
	if err != nil {
		return memberList{}, fmt.Errorf("read group members: %w", err)
	}
	if response.StatusCode != http.StatusOK {
		return memberList{}, fmt.Errorf("list group members: status %d", response.StatusCode)
	}
	var list memberList
	if err := json.Unmarshal(body, &list); err != nil {
		return memberList{}, fmt.Errorf("decode group members: %w", err)
	}
	return list, nil
}

// validMemberID accepts Google's numeric user id. It is compared as text, not as
// a number: ids are already 21 digits and would overflow a uint64.
func validMemberID(id string) bool {
	if id == "" || len(id) > 21 {
		return false
	}
	for _, r := range id {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// digest makes the revision depend on content only, so an unchanged group keeps
// the same revision across runs.
func digest(tuples []relationships.Tuple) string {
	h := sha256.New()
	for _, tuple := range tuples {
		_, _ = io.WriteString(h, tuple.Key())
		_, _ = io.WriteString(h, "\n")
	}
	return "sha256:" + hex.EncodeToString(h.Sum(nil))[:16]
}
