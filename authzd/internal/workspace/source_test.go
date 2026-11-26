package workspace

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func tokenSource(value string) func(context.Context) (string, error) {
	return func(context.Context) (string, error) { return value, nil }
}

type page struct {
	members       []member
	nextPageToken string
}

func newGroupServer(t *testing.T, pages []page) (*httptest.Server, *[]string) {
	t.Helper()
	var requests []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer test-token" {
			t.Errorf("Authorization = %q", got)
		}
		if !strings.HasPrefix(r.URL.Path, "/admin/directory/v1/groups/all@example.com/members") {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		requests = append(requests, r.URL.RawQuery)
		index := 0
		if token := r.URL.Query().Get("pageToken"); token != "" {
			index = 1
		}
		if index >= len(pages) {
			t.Errorf("unexpected page request %q", r.URL.RawQuery)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"members":       pages[index].members,
			"nextPageToken": pages[index].nextPageToken,
		})
	}))
	t.Cleanup(server.Close)
	return server, &requests
}

func TestFetchMapsUserMembersToSubjects(t *testing.T) {
	server, requests := newGroupServer(t, []page{{
		members: []member{
			{ID: "102401699953594095413", Email: "a@example.com", Type: "USER", Status: "ACTIVE"},
			{ID: "113364093292557124527", Email: "b@example.com", Type: "USER", Status: "ACTIVE"},
			{ID: "012345678901234567890", Email: "group@example.com", Type: "GROUP", Status: "ACTIVE"},
			{ID: "098765432109876543210", Email: "old@example.com", Type: "USER", Status: "ARCHIVED"},
		},
	}})
	source, err := NewSource(Config{
		BaseURL: server.URL, GroupKey: "all@example.com", TeamObject: "team:all-staff",
		Token: tokenSource("test-token"),
	})
	if err != nil {
		t.Fatalf("NewSource() error = %v", err)
	}
	snapshot, err := source.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch() error = %v", err)
	}
	if !snapshot.Complete {
		t.Fatal("a single fully-read page must be complete")
	}
	if len(snapshot.Tuples) != 2 {
		t.Fatalf("tuples = %+v, want two active user members", snapshot.Tuples)
	}
	if snapshot.Tuples[0].User != "user:google/sub/102401699953594095413" ||
		snapshot.Tuples[0].Relation != "member" || snapshot.Tuples[0].Object != "team:all-staff" {
		t.Fatalf("tuple = %+v", snapshot.Tuples[0])
	}
	if snapshot.Source != "workspace-group/all@example.com" {
		t.Fatalf("Source = %q", snapshot.Source)
	}
	if !strings.HasPrefix(snapshot.Revision, "sha256:") {
		t.Fatalf("Revision = %q, want a content digest", snapshot.Revision)
	}
	if snapshot.GeneratedAt.IsZero() {
		t.Fatal("GeneratedAt is zero")
	}
	if len(*requests) != 1 {
		t.Fatalf("requests = %d", len(*requests))
	}
}

func TestFetchFollowsPaginationAndDigestsContent(t *testing.T) {
	server, _ := newGroupServer(t, []page{
		{members: []member{{ID: "1", Type: "USER", Status: "ACTIVE"}}, nextPageToken: "p2"},
		{members: []member{{ID: "2", Type: "USER", Status: "ACTIVE"}}},
	})
	source, err := NewSource(Config{
		BaseURL: server.URL, GroupKey: "all@example.com", TeamObject: "team:all-staff",
		Token: tokenSource("test-token"),
	})
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := source.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch() error = %v", err)
	}
	if len(snapshot.Tuples) != 2 {
		t.Fatalf("tuples = %d, want 2 across two pages", len(snapshot.Tuples))
	}

	// The revision must depend on content only, so a reconcile can tell an
	// unchanged group from a changed one.
	again, err := source.Fetch(context.Background())
	if err != nil {
		t.Fatalf("second Fetch() error = %v", err)
	}
	if again.Revision != snapshot.Revision {
		t.Fatalf("revision changed without a content change: %q vs %q", snapshot.Revision, again.Revision)
	}
}

func TestFetchFailsClosedOnTransportAndContentErrors(t *testing.T) {
	server, _ := newGroupServer(t, []page{{members: []member{{ID: "1", Type: "USER", Status: "ACTIVE"}}}})
	ok := Config{BaseURL: server.URL, GroupKey: "all@example.com", TeamObject: "team:all-staff", Token: tokenSource("test-token")}

	failing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer failing.Close()

	cases := map[string]struct {
		cfg  Config
		want string
	}{
		"token failure": {
			cfg: func() Config {
				c := ok
				c.Token = func(context.Context) (string, error) { return "", errors.New("no token") }
				return c
			}(),
			want: "credential",
		},
		"transport failure": {
			cfg:  func() Config { c := ok; c.BaseURL = failing.URL; return c }(),
			want: "list group members",
		},
		"malformed member id": {
			cfg:  ok,
			want: "non-numeric",
		},
	}
	for name, testCase := range cases {
		t.Run(name, func(t *testing.T) {
			if name == "malformed member id" {
				bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
					_, _ = w.Write([]byte(`{"members":[{"id":"not-a-number","type":"USER","status":"ACTIVE"}]}`))
				}))
				defer bad.Close()
				testCase.cfg.BaseURL = bad.URL
			}
			source, err := NewSource(testCase.cfg)
			if err != nil {
				t.Fatalf("NewSource() error = %v", err)
			}
			if _, err := source.Fetch(context.Background()); err == nil || !strings.Contains(err.Error(), testCase.want) {
				t.Fatalf("Fetch() error = %v, want one containing %q", err, testCase.want)
			}
		})
	}
}

func TestNewSourceRequiresConfiguration(t *testing.T) {
	base := Config{GroupKey: "all@example.com", TeamObject: "team:all-staff", Token: tokenSource("t")}
	for name, mutate := range map[string]func(*Config){
		"missing group":   func(c *Config) { c.GroupKey = "" },
		"missing team":    func(c *Config) { c.TeamObject = "" },
		"missing token":   func(c *Config) { c.Token = nil },
		"bad team object": func(c *Config) { c.TeamObject = "team" },
		"bad base url":    func(c *Config) { c.BaseURL = "file:///tmp/x" },
	} {
		t.Run(name, func(t *testing.T) {
			cfg := base
			mutate(&cfg)
			if _, err := NewSource(cfg); err == nil {
				t.Fatal("NewSource() accepted an invalid configuration")
			}
		})
	}
}

func TestFetchStopsRunawayPagination(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		_, _ = w.Write([]byte(`{"members":[],"nextPageToken":"same"}`))
	}))
	defer server.Close()
	source, err := NewSource(Config{
		BaseURL: server.URL, GroupKey: "all@example.com", TeamObject: "team:all-staff",
		Token: tokenSource("t"), MaxPages: 3,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := source.Fetch(context.Background()); err == nil {
		t.Fatal("Fetch() followed pagination past the configured bound")
	}
	if calls > 3 {
		t.Fatalf("page requests = %d, want at most 3", calls)
	}
}
