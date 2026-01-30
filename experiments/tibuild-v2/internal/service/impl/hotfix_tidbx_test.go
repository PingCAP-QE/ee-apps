package impl

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/google/go-github/v69/github"
	"github.com/migueleliasweb/go-github-mock/src/mock"
	"github.com/rs/zerolog"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
)

func newServiceWithClient(client *github.Client) *hotfixsrvc {
	logger := zerolog.New(io.Discard)
	return &hotfixsrvc{
		logger:   &logger,
		ghClient: client,
	}
}

func TestComputeNewTagNameForTidbx(t *testing.T) {
	type testCase struct {
		name            string
		pages           [][]string
		expected        string
		taggedCommitSHA string
		behindCommitSHA string
		expectErr       bool
		errCode         int
	}

	testCommitSHA := "a9814602ed087838d71095efd35bd221ab0bf5a9"
	behindCommitSHA := "bbbb1234567890abcdef1234567890abcdef1234"
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
					"v8.5.4",
					"v8.5.4-nextgen.202512",
					"some-other-tag",
					"vX.Y.Z-nextgen.202512.abc",
					"release-20251201",
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
					"v9.0.0-nextgen.202601.0",
				},
			},
			expected: "v9.0.0-nextgen.202601.1",
		},
		{
			name: "CommitAlreadyTagged",
			pages: [][]string{
				{
					"v8.5.4-nextgen.202510.1",
					"v8.5.4-nextgen.202510.2",
				},
			},
			taggedCommitSHA: testCommitSHA,
			expectErr:       true,
			errCode:         http.StatusBadRequest,
		},
		{
			name: "CommitBehindExistingTag",
			pages: [][]string{
				{
					"v8.5.4-nextgen.202510.1",
					"v8.5.4-nextgen.202510.2",
				},
			},
			behindCommitSHA: behindCommitSHA,
			expectErr:       true,
			errCode:         http.StatusBadRequest,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// Build mocked pages for ListTags
			var tagPages []any

			for _, names := range tc.pages {
				tags := make([]*github.RepositoryTag, len(names))
				for j, name := range names {
					tags[j] = &github.RepositoryTag{Name: github.Ptr(name)}
					// For the CommitAlreadyTagged case, attach the commit SHA to the first tidbx-style tag
					if j == 0 && tc.taggedCommitSHA != "" && strings.HasPrefix(name, "v") && strings.Contains(name, "-nextgen.") {
						tags[j].Commit = &github.Commit{SHA: &tc.taggedCommitSHA}
					}
				}
				tagPages = append(tagPages, tags)
			}
			resp := mock.WithRequestMatchPages(mock.GetReposTagsByOwnerByRepo, tagPages...)

			// Mock CompareCommits for the CommitBehindExistingTag case
			var comparisonResponse *github.CommitsComparison
			if tc.behindCommitSHA != "" {
				comparisonResponse = &github.CommitsComparison{
					Status:   github.Ptr("behind"),
					BehindBy: github.Ptr(1),
				}
			} else {
				comparisonResponse = &github.CommitsComparison{
					Status:   github.Ptr("identical"),
					BehindBy: github.Ptr(0),
				}
			}
			compareMock := mock.WithRequestMatch(mock.GetReposCompareByOwnerByRepoByBasehead, comparisonResponse)

			ghClient := github.NewClient(mock.NewMockedHTTPClient(resp, compareMock))
			svc := newServiceWithClient(ghClient)

			commitSHA := testCommitSHA
			if tc.behindCommitSHA != "" {
				commitSHA = tc.behindCommitSHA
			}

			tag, err := svc.computeNewTagNameForTidbx(context.Background(), "owner", "repo", commitSHA)
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

func TestBumpTagForTidbx_PaginationFlow(t *testing.T) {
	fullRepo := "owner/repo"

	// Tags response for pagination flow
	respTags := mock.WithRequestMatchPages(
		mock.GetReposTagsByOwnerByRepo,
		[]*github.RepositoryTag{
			{Name: github.Ptr("v9.0.0-nextgen.202401.2")},
			{Name: github.Ptr("v9.0.0-nextgen.202401.1")},
			{Name: github.Ptr("v9.0.0-nextgen.202312.7")},
		},
		[]*github.RepositoryTag{
			{Name: github.Ptr("v9.0.0-nextgen.202511.3")},
			{Name: github.Ptr("v9.0.0-nextgen.202511.5")},
			{Name: github.Ptr("v9.0.0-nextgen.202601.0")},
		},
	)

	branch := "main"
	commit := "abc123"
	expectedTag := "v9.0.0-nextgen.202601.1"

	type args struct {
		fullRepo string
		branch   string
		commit   string
	}
	tests := []struct {
		name          string
		args          args
		existTagPages [][]*github.RepositoryTag
		expectTag     string
	}{
		{
			name:      "PaginationFlow with branch",
			args:      args{fullRepo: fullRepo, branch: branch},
			expectTag: expectedTag,
		},
		{
			name:      "PaginationFlow with commit",
			args:      args{fullRepo: fullRepo, commit: commit},
			expectTag: expectedTag,
		},
		{
			name:      "PaginationFlow with branch and commit",
			args:      args{fullRepo: fullRepo, branch: branch, commit: commit},
			expectTag: expectedTag,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(tt *testing.T) {
			// Prepare mocked responses:
			// - GET tags pages
			// - GET branch
			// - POST create tag
			// - POST create ref

			httpClient := mock.NewMockedHTTPClient(
				respTags,
				mock.WithRequestMatch(
					mock.GetReposCommitsByOwnerByRepoByRef,
					&github.RepositoryCommit{
						SHA: github.Ptr(commit),
					},
				),
				mock.WithRequestMatch(
					mock.GetReposBranchesByOwnerByRepoByBranch,
					&github.Branch{
						Name: github.Ptr(branch),
						Commit: &github.RepositoryCommit{
							SHA: github.Ptr(commit),
						},
					},
				),
				mock.WithRequestMatch(
					mock.PostReposGitTagsByOwnerByRepo,
					&github.Tag{
						Tag: github.Ptr("v9.0.0-nextgen.202601.1"),
						// Message is now JSON metadata (see `tidbxTagMeta` in `hotfix_tidbx.go`)
						Message: github.Ptr(func() string {
							b, _ := json.Marshal(map[string]any{
								"author": "tester",
								"meta": map[string]any{
									"ops_req": map[string]any{
										"applicant":  "tester",
										"release_id": "rw-12345",
										"change_id":  "ch-67890",
									},
								},
							})
							return string(b)
						}()),
						Object: &github.GitObject{
							Type: github.Ptr("commit"),
							SHA:  github.Ptr(commit),
						},
						SHA: github.Ptr(commit),
					},
				),
				mock.WithRequestMatch(
					mock.PostReposGitRefsByOwnerByRepo,
					&github.Reference{
						Ref: github.Ptr("refs/tags/v9.0.0-nextgen.202601.1"),
						Object: &github.GitObject{
							SHA: github.Ptr(commit),
						},
					},
				),
				mock.WithRequestMatch(
					mock.GetReposCompareByOwnerByRepoByBasehead,
					&github.CommitsComparison{Status: github.Ptr("identical")},
					&github.CommitsComparison{Status: github.Ptr("ahead")},
				),
			)
			svc := newServiceWithClient(github.NewClient(httpClient))

			// prepare api payload
			apiCallPayload := &hotfix.BumpTagForTidbxPayload{
				Repo:   fullRepo,
				Author: "tester",
			}
			if test.args.commit != "" {
				apiCallPayload.Commit = &test.args.commit
			}
			if test.args.branch != "" {
				apiCallPayload.Branch = &test.args.branch
			}

			// call the api
			result, err := svc.BumpTagForTidbx(tt.Context(), apiCallPayload)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			if result.Repo != test.args.fullRepo {
				t.Fatalf("expected repo %s, got %s", test.args.fullRepo, result.Repo)
			}
			if result.Commit != commit {
				t.Fatalf("expected commit %s, got %s", commit, result.Commit)
			}
			if result.Tag != test.expectTag {
				t.Fatalf("expected tag %s, got %s", test.expectTag, result.Tag)
			}
		})
	}
}

func TestBumpTagForTidbx_FailWhenCommitAlreadyTagged(t *testing.T) {
	fullRepo := "owner/repo"
	branch := "main"
	commit := "abc123"

	// Tags response includes a tidbx-style tag that points to the same commit
	respTags := mock.WithRequestMatchPages(
		mock.GetReposTagsByOwnerByRepo,
		[]*github.RepositoryTag{
			{Name: github.Ptr("v8.5.4-nextgen.202510.1"), Commit: &github.Commit{SHA: github.Ptr(commit)}},
			{Name: github.Ptr("v8.5.4-nextgen.202510.2")},
		},
	)

	httpClient := mock.NewMockedHTTPClient(
		respTags,
		mock.WithRequestMatch(
			mock.GetReposCommitsByOwnerByRepoByRef,
			&github.RepositoryCommit{
				SHA: github.Ptr(commit),
			},
		),
		mock.WithRequestMatch(
			mock.GetReposBranchesByOwnerByRepoByBranch,
			&github.Branch{
				Name: github.Ptr(branch),
				Commit: &github.RepositoryCommit{
					SHA: github.Ptr(commit),
				},
			},
		),
	)
	svc := newServiceWithClient(github.NewClient(httpClient))

	apiCallPayload := &hotfix.BumpTagForTidbxPayload{
		Repo:   fullRepo,
		Author: "tester",
		Branch: &branch,
		Commit: &commit,
	}

	_, err := svc.BumpTagForTidbx(context.Background(), apiCallPayload)
	if err == nil {
		t.Fatalf("expected error due to existing tidbx-style tag on commit, got nil")
	}
	httpErr, ok := err.(*hotfix.HTTPError)
	if !ok {
		t.Fatalf("expected *hotfix.HTTPError, got %T", err)
	}
	if httpErr.Code != http.StatusBadRequest {
		t.Fatalf("expected status %d, got %d", http.StatusBadRequest, httpErr.Code)
	}
}

func TestQueryTagOfTidbx_ParseJSONMetadata(t *testing.T) {
	fullRepo := "owner/repo"
	tag := "v8.5.4-nextgen.202510.1"

	wantAuthor := "tester@example.com"
	wantReleaseID := "rw-12345"
	wantChangeID := "ch-67890"

	metaBytes, err := json.Marshal(map[string]any{
		"author": wantAuthor,
		"meta": map[string]any{
			"ops_req": map[string]any{
				"applicant":  wantAuthor,
				"release_id": wantReleaseID,
				"change_id":  wantChangeID,
			},
		},
	})
	if err != nil {
		t.Fatalf("failed to marshal metadata json: %v", err)
	}

	httpClient := mock.NewMockedHTTPClient(
		// `QueryTagOfTidbx` now calls `s.getTag(...)` which does:
		// 1) Git.GetRef("tags/<tag>")
		// 2) Git.GetTag(<sha from ref>)
		mock.WithRequestMatch(
			mock.GetReposGitRefByOwnerByRepoByRef,
			&github.Reference{
				Ref: github.Ptr("refs/tags/" + tag),
				Object: &github.GitObject{
					SHA: github.Ptr("deadbeef"),
				},
			},
		),
		mock.WithRequestMatch(
			mock.GetReposGitTagsByOwnerByRepoByTagSha,
			&github.Tag{
				Tag:     github.Ptr(tag),
				Message: github.Ptr(string(metaBytes)),
				SHA:     github.Ptr("deadbeef"),
				Object: &github.GitObject{
					Type: github.Ptr("commit"),
					SHA:  github.Ptr("abc123"),
				},
			},
		),
	)

	svc := newServiceWithClient(github.NewClient(httpClient))

	res, qerr := svc.QueryTagOfTidbx(context.Background(), &hotfix.QueryTagOfTidbxPayload{
		Repo: fullRepo,
		Tag:  tag,
	})
	if qerr != nil {
		t.Fatalf("unexpected error: %v", qerr)
	}

	if res.Repo != fullRepo {
		t.Fatalf("expected repo %s, got %s", fullRepo, res.Repo)
	}
	if res.Tag != tag {
		t.Fatalf("expected tag %s, got %s", tag, res.Tag)
	}
	// Query uses the tag object SHA as Commit (per current implementation).
	if res.Commit != "deadbeef" {
		t.Fatalf("expected commit %s, got %s", "deadbeef", res.Commit)
	}
	if res.Author == nil || *res.Author != wantAuthor {
		t.Fatalf("expected author %q, got %+v", wantAuthor, res.Author)
	}

	if res.Meta == nil || res.Meta.OpsReq == nil || res.Meta.OpsReq.ReleaseID == nil || *res.Meta.OpsReq.ReleaseID != wantReleaseID {
		t.Fatalf("expected release_id %q, got %+v", wantReleaseID, res.Meta.OpsReq)
	}
	if res.Meta == nil || res.Meta.OpsReq == nil || res.Meta.OpsReq.ChangeID == nil || *res.Meta.OpsReq.ChangeID != wantChangeID {
		t.Fatalf("expected change_id %q, got %+v", wantChangeID, res.Meta)
	}
}

func TestQueryTagOfTidbx_InvalidMetadataDoesNotFail(t *testing.T) {
	fullRepo := "owner/repo"
	tag := "v8.5.4-nextgen.202510.1"

	httpClient := mock.NewMockedHTTPClient(
		// `QueryTagOfTidbx` now calls `s.getTag(...)` which does:
		// 1) Git.GetRef("tags/<tag>")
		// 2) Git.GetTag(<sha from ref>)
		mock.WithRequestMatch(
			mock.GetReposGitRefByOwnerByRepoByRef,
			&github.Reference{
				Ref: github.Ptr("refs/tags/" + tag),
				Object: &github.GitObject{
					SHA: github.Ptr("deadbeef"),
				},
			},
		),
		mock.WithRequestMatch(
			mock.GetReposGitTagsByOwnerByRepoByTagSha,
			&github.Tag{
				Tag:     github.Ptr(tag),
				Message: github.Ptr("{not-json"),
				SHA:     github.Ptr("deadbeef"),
				Object: &github.GitObject{
					Type: github.Ptr("commit"),
					SHA:  github.Ptr("abc123"),
				},
			},
		),
	)

	svc := newServiceWithClient(github.NewClient(httpClient))

	res, qerr := svc.QueryTagOfTidbx(context.Background(), &hotfix.QueryTagOfTidbxPayload{
		Repo: fullRepo,
		Tag:  tag,
	})
	if qerr != nil {
		t.Fatalf("unexpected error: %v", qerr)
	}
	if res.Author != nil || res.Meta != nil {
		t.Fatalf("expected nil metadata fields on invalid json, got author=%+v meta=%+v", res.Author, res.Meta)
	}
}
