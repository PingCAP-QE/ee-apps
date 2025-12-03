package impl

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	"github.com/google/go-github/v69/github"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
)

// helper to create a github client backed by a test server that serves paginated tag lists
func newTestClientWithTagPages(t *testing.T, pages [][]string) (*github.Client, func()) {
	t.Helper()

	mux := http.NewServeMux()
	mux.HandleFunc("/repos/owner/repo/tags", func(w http.ResponseWriter, r *http.Request) {
		// Determine page number, default to 1
		page := 1
		if p := r.URL.Query().Get("page"); p != "" {
			fmt.Sscanf(p, "%d", &page)
		}

		// If page is out of range, return empty
		var names []string
		if page >= 1 && page <= len(pages) {
			names = pages[page-1]
		}

		// Build response objects
		var tags []*github.RepositoryTag
		for _, name := range names {
			tags = append(tags, &github.RepositoryTag{
				Name: github.Ptr(name),
			})
		}

		enc := json.NewEncoder(w)
		// Set pagination Link header if there is a next page
		if page < len(pages) {
			// Simulate GitHub pagination Link header
			// Only rel="next" is needed for NextPage to be parsed
			nextURL := url.URL{
				Path: "/repos/owner/repo/tags",
				RawQuery: url.Values{
					"page":     []string{fmt.Sprintf("%d", page+1)},
					"per_page": []string{"100"},
				}.Encode(),
			}
			w.Header().Set("Link", fmt.Sprintf("<%s>; rel=\"next\"", nextURL.String()))
		}
		w.WriteHeader(http.StatusOK)
		_ = enc.Encode(tags)
	})

	server := httptest.NewServer(mux)

	client := github.NewClient(nil)
	base, err := url.Parse(server.URL + "/")
	if err != nil {
		t.Fatalf("failed to parse server URL: %v", err)
	}
	client.BaseURL = base
	client.UploadURL = base

	cleanup := func() {
		server.Close()
	}

	return client, cleanup
}

func newServiceWithClient(client *github.Client) *hotfixsrvc {
	logger := zerolog.New(nil)
	return &hotfixsrvc{
		logger:   &logger,
		ghClient: client,
	}
}

func TestComputeNewTagNameForTidbx(t *testing.T) {
	type testCase struct {
		name      string
		pages     [][]string
		expected  string
		expectErr bool
		errCode   int
	}

	cases := []testCase{
		{
			name: "LastMonthIncrement",
			pages: [][]string{
				{
					"v8.5.4-nextgen.202510.1",
					"v8.5.4-nextgen.202510.3",
					"v8.5.4-nextgen.202510.2",
					"v8.5.4-nextgen.202510.foo",
					"random-tag",
				},
			},
			expected: "v8.5.4-nextgen.202510.4",
		},
		{
			name: "Have tags with two months",
			pages: [][]string{
				{
					"v7.1.0-nextgen.202410.10",
					"v7.1.0-nextgen.202410.9",
					"v7.1.0-nextgen.202409.3",
				},
			},
			expected: "v7.1.0-nextgen.202410.11",
		},
		{
			name: "NoMatchingTags",
			pages: [][]string{
				{
					"v8.5.4",                    // missing nextgen pattern
					"v8.5.4-nextgen.202512",     // missing sequence
					"some-other-tag",            // random
					"vX.Y.Z-nextgen.202512.abc", // invalid sequence
					"release-20251201",          // not matching
				},
			},
			expectErr: true,
			errCode:   http.StatusBadRequest,
		},
		{
			name: "Pagination",
			pages: [][]string{
				{
					"v9.0.0-nextgen.202401.2",
					"v9.0.0-nextgen.202401.1",
					"v9.0.0-nextgen.202312.7",
				},
				{
					"v9.0.0-nextgen.202511.3",
					"v9.0.0-nextgen.202511.5",
					"v9.0.0-nextgen.202511.4",
				},
			},
			expected: "v9.0.0-nextgen.202511.6",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			client, cleanup := newTestClientWithTagPages(t, tc.pages)
			defer cleanup()

			svc := newServiceWithClient(client)

			tag, err := svc.computeNewTagNameForTidbx(context.Background(), "owner", "repo")
			if tc.expectErr {
				if err == nil {
					t.Fatalf("expected error, got nil")
				}
				httpErr, ok := err.(*hotfix.HTTPError)
				if !ok {
					t.Fatalf("expected *hotfix.HTTPError, got %T", err)
				}
				if httpErr.Code != tc.errCode {
					t.Fatalf("expected status %d, got %d", tc.errCode, httpErr.Code)
				}
				return
			}

			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if tag != tc.expected {
				t.Fatalf("expected %s, got %s", tc.expected, tag)
			}
		})
	}
}
