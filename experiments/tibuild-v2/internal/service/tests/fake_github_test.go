package impl_test

import (
	"crypto/sha1" //nolint:gosec // deterministic fake refs only, not a security primitive
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
)

// fakeGithub serves the handful of GitHub REST endpoints tibuild-v2 uses to
// resolve devbuild git refs. It exists so the service tests never talk to
// api.github.com.
type fakeGithub struct {
	server *httptest.Server

	mu         sync.Mutex
	requests   int
	failStatus int
	missing    map[string]bool
}

func newFakeGithub(t *testing.T) *fakeGithub {
	t.Helper()
	f := &fakeGithub{missing: map[string]bool{}}
	f.server = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.server.Close)
	return f
}

// reset zeroes the request counter, leaving the configured failure/missing
// state intact.
func (f *fakeGithub) reset() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = 0
}

func (f *fakeGithub) requestCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.requests
}

// setFailStatus makes every subsequent request return the given HTTP status,
// simulating auth failures, rate limits and GitHub 5xx responses.
func (f *fakeGithub) setFailStatus(status int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.failStatus = status
}

// markMissing marks "<owner>/<repo>|<kind>/<name>" as absent, where kind is
// branch, tag, pull or commit.
func (f *fakeGithub) markMissing(repo, ref string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.missing[repo+"|"+ref] = true
}

func (f *fakeGithub) isMissing(key string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.missing[key]
}

func (f *fakeGithub) handle(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.requests++
	status := f.failStatus
	f.mu.Unlock()

	if status != 0 {
		writeGitHubError(w, status)
		return
	}

	path := strings.TrimPrefix(r.URL.Path, "/")
	parts := strings.SplitN(path, "/", 4)
	if len(parts) != 4 || parts[0] != "repos" {
		writeGitHubError(w, http.StatusNotFound)
		return
	}
	repo := parts[1] + "/" + parts[2]
	rest := parts[3]

	switch {
	case strings.HasPrefix(rest, "git/ref/heads/"):
		branch := strings.TrimPrefix(rest, "git/ref/heads/")
		key := repo + "|branch/" + branch
		if f.isMissing(key) {
			writeGitHubError(w, http.StatusNotFound)
			return
		}
		writeGitHubJSON(w, http.StatusOK, map[string]any{
			"ref":    "refs/heads/" + branch,
			"object": map[string]string{"sha": fakeSHA(key)},
		})
	case strings.HasPrefix(rest, "git/ref/tags/"):
		tag := strings.TrimPrefix(rest, "git/ref/tags/")
		key := repo + "|tag/" + tag
		if f.isMissing(key) {
			writeGitHubError(w, http.StatusNotFound)
			return
		}
		writeGitHubJSON(w, http.StatusOK, map[string]any{
			"ref":    "refs/tags/" + tag,
			"object": map[string]string{"sha": fakeSHA(key)},
		})
	case strings.HasPrefix(rest, "pulls/"):
		number := strings.TrimPrefix(rest, "pulls/")
		key := repo + "|pull/" + number
		if f.isMissing(key) {
			writeGitHubError(w, http.StatusNotFound)
			return
		}
		n, _ := strconv.Atoi(number)
		writeGitHubJSON(w, http.StatusOK, map[string]any{
			"number": n,
			"head":   map[string]string{"sha": fakeSHA(key)},
			"base":   map[string]string{"ref": "master"},
		})
	case strings.HasPrefix(rest, "commits/"):
		sha := strings.TrimPrefix(rest, "commits/")
		key := repo + "|commit/" + sha
		if f.isMissing(key) {
			writeGitHubError(w, http.StatusNotFound)
			return
		}
		writeGitHubJSON(w, http.StatusOK, map[string]any{"sha": sha})
	default:
		writeGitHubError(w, http.StatusNotFound)
	}
}

func fakeSHA(key string) string {
	sum := sha1.Sum([]byte(key)) //nolint:gosec // fake data only
	return hex.EncodeToString(sum[:])
}

func writeGitHubJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeGitHubError(w http.ResponseWriter, status int) {
	writeGitHubJSON(w, status, map[string]string{"message": http.StatusText(status)})
}

// cloudEventSink counts CloudEvent deliveries and keeps the last payload so
// tests can assert on body.after.
type cloudEventSink struct {
	mu       sync.Mutex
	requests int
	lastBody []byte
	eventID  string
}

func (s *cloudEventSink) handle(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	s.mu.Lock()
	s.requests++
	s.lastBody = body
	eventID := s.eventID
	s.mu.Unlock()

	if eventID != "" {
		// Return a CloudEvent so the client can capture an event ID.
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("ce-specversion", "1.0")
		w.Header().Set("ce-id", eventID)
		w.Header().Set("ce-source", "/test")
		w.Header().Set("ce-type", "net.pingcap.tibuild.test")
		_, _ = w.Write([]byte(`{}`))
		return
	}
	w.WriteHeader(http.StatusOK)
}

// setEventID makes the sink acknowledge events with the given CloudEvent ID.
func (s *cloudEventSink) setEventID(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.eventID = id
}

func (s *cloudEventSink) reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.requests = 0
	s.lastBody = nil
}

func (s *cloudEventSink) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.requests
}

func (s *cloudEventSink) body() []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]byte(nil), s.lastBody...)
}
