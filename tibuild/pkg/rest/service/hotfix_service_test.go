package service

import (
	"context"
	"github.com/stretchr/testify/assert"
	"strconv"
	"testing"
	"time"
)

func TestGenTwoNumVersion(t *testing.T) {
	tests := []struct {
		Version  string
		Expected string
	}{
		{Version: "v5.1.5", Expected: "5.1"},
		{Version: "v10.2.0", Expected: "10.2"},
		{Version: "v5.12.7", Expected: "5.12"},
		{Version: "5.3.1", Expected: ""},
		{Version: "vv5.2.1", Expected: ""},
	}
	for index, testcase := range tests {
		t.Run(strconv.Itoa(index), func(t *testing.T) {
			actual := GenTwoNumVersion(testcase.Version)
			assert.Equal(t, testcase.Expected, actual)
		})
	}
}

func TestBranchName(t *testing.T) {
	tests := []struct {
		Version  string
		Date     string
		Expected string
	}{
		{Version: "v5.1.5", Date: "20220904", Expected: "release-5.1-20220904-v5.1.5"},
		{Version: "v5.10.0", Date: "20220904", Expected: "release-5.10-20220904-v5.10.0"},
		{Version: "5.3.1", Expected: ""},
		{Version: "vv5.2.1", Expected: ""},
	}
	for index, testcase := range tests {
		t.Run(strconv.Itoa(index), func(t *testing.T) {
			actual := BranchName(testcase.Version, testcase.Date)
			assert.Equal(t, testcase.Expected, actual)
		})
	}
}

func TestTagName(t *testing.T) {
	tests := []struct {
		Branch   string
		Date     string
		Expected string
	}{
		{Branch: "release-5.1-20220904-v5.1.5", Date: "20220905", Expected: "v5.1.5-20220905"},
		{Branch: "release-5.10-20220904-v5.10.0", Date: "20220905", Expected: "v5.10.0-20220905"},
		{Branch: "release-5.10-20220904", Expected: ""},
		{Branch: "vv5.2.1", Expected: ""},
	}
	for index, testcase := range tests {
		t.Run(strconv.Itoa(index), func(t *testing.T) {
			actual := TagName(testcase.Branch, testcase.Date)
			assert.Equal(t, testcase.Expected, actual)
		})
	}
}

type branchCreatorStub struct {
	tagURL     string
	branchName string

	branchURL string
	tag       string

	err error
}

func (b *branchCreatorStub) CreateTagFromBranch(ctx context.Context, repo GithubRepo, tag string, branch string) error {
	b.branchURL = branch
	b.tag = tag
	return b.err
}

func (b *branchCreatorStub) CreateBranchFromTag(ctx context.Context, repo GithubRepo, branch string, tag string) error {
	b.tagURL = tag
	b.branchName = branch
	return b.err
}

func (b *branchCreatorStub) GetCommitSHA(ctx context.Context, repo GithubRepo, ref string) (string, error) {
	return "", b.err
}

func (b *branchCreatorStub) ListTags(ctx context.Context, repo GithubRepo) ([]string, error) {
	return []string{}, b.err
}

func (b *branchCreatorStub) CreateAnnotatedTag(ctx context.Context, repo GithubRepo, tag string, commit string, message string) error {
	return b.err
}

func (b *branchCreatorStub) GetBranchesForCommit(ctx context.Context, repo GithubRepo, commit string) ([]string, error) {
	return []string{}, b.err
}

func TestRepoService_CreateBranch(t *testing.T) {
	s := repoService{now: func() time.Time {
		return time.Date(2022, 9, 4, 0, 0, 0, 0, time.UTC)
	}, branchCreator: &branchCreatorStub{}}
	branch, err := s.CreateBranch(context.TODO(), BranchCreateReq{Prod: ProductTidb, BaseVersion: "v6.1.1"})
	assert.NoError(t, err)
	assert.Equal(t, &BranchCreateResp{
		Branch:    "release-6.1-20220904-v6.1.1",
		BranchURL: "https://github.com/pingcap/tidb/tree/release-6.1-20220904-v6.1.1"}, branch)
}

func TestRepoService_CreateTag(t *testing.T) {
	s := repoService{now: func() time.Time {
		return time.Date(2022, 9, 4, 0, 0, 0, 0, time.UTC)
	}, branchCreator: &branchCreatorStub{}}
	branch, err := s.CreateTag(context.TODO(), TagCreateReq{Prod: ProductTiflash, Branch: "release-6.1-20220904-v6.1.1"})
	assert.NoError(t, err)
	assert.Equal(t, &TagCreateResp{
		Tag:    "v6.1.1-20220904",
		TagURL: "https://github.com/pingcap/tiflash/releases/tag/v6.1.1-20220904"}, branch)
}

func TestComputeNextgenTagName(t *testing.T) {
	tests := []struct {
		name     string
		tags     []string
		now      time.Time
		expected string
	}{
		{
			name: "no existing tags for current month",
			tags: []string{
				"v8.5.4-nextgen.202510.10",
				"v8.5.4-nextgen.202510.9",
			},
			now:      time.Date(2025, 11, 1, 0, 0, 0, 0, time.UTC),
			expected: "v8.5.4-nextgen.202511.1",
		},
		{
			name: "existing tags for current month",
			tags: []string{
				"v8.5.4-nextgen.202510.10",
				"v8.5.4-nextgen.202510.9",
				"v8.5.4-nextgen.202510.1",
			},
			now:      time.Date(2025, 10, 1, 0, 0, 0, 0, time.UTC),
			expected: "v8.5.4-nextgen.202510.11",
		},
		{
			name: "mixed versions in current month",
			tags: []string{
				"v8.5.4-nextgen.202510.5",
				"v8.5.3-nextgen.202510.3",
				"v8.5.4-nextgen.202510.10",
			},
			now:      time.Date(2025, 10, 1, 0, 0, 0, 0, time.UTC),
			expected: "v8.5.4-nextgen.202510.11",
		},
		{
			name:     "no tags at all",
			tags:     []string{},
			now:      time.Date(2025, 12, 1, 0, 0, 0, 0, time.UTC),
			expected: "v8.5.4-nextgen.202512.1",
		},
		{
			name: "tags from different months",
			tags: []string{
				"v8.5.4-nextgen.202509.10",
				"v8.5.4-nextgen.202508.9",
			},
			now:      time.Date(2025, 10, 1, 0, 0, 0, 0, time.UTC),
			expected: "v8.5.4-nextgen.202510.1",
		},
		{
			name: "tags with non-matching format",
			tags: []string{
				"v8.5.4",
				"v8.5.4-20220904",
				"v8.5.4-nextgen.202510.5",
			},
			now:      time.Date(2025, 10, 1, 0, 0, 0, 0, time.UTC),
			expected: "v8.5.4-nextgen.202510.6",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			actual, err := ComputeNextgenTagName(tt.tags, tt.now)
			assert.NoError(t, err)
			assert.Equal(t, tt.expected, actual)
		})
	}
}

type branchCreatorMock struct {
	getSHAFunc         func(ctx context.Context, repo GithubRepo, ref string) (string, error)
	listTagsFunc       func(ctx context.Context, repo GithubRepo) ([]string, error)
	createTagFunc      func(ctx context.Context, repo GithubRepo, tag string, commit string, message string) error
	getBranchesFunc    func(ctx context.Context, repo GithubRepo, commit string) ([]string, error)
}

func (m *branchCreatorMock) CreateBranchFromTag(ctx context.Context, repo GithubRepo, branch string, tag string) error {
	return nil
}

func (m *branchCreatorMock) CreateTagFromBranch(ctx context.Context, repo GithubRepo, tag string, branch string) error {
	return nil
}

func (m *branchCreatorMock) GetCommitSHA(ctx context.Context, repo GithubRepo, ref string) (string, error) {
	if m.getSHAFunc != nil {
		return m.getSHAFunc(ctx, repo, ref)
	}
	return "", nil
}

func (m *branchCreatorMock) ListTags(ctx context.Context, repo GithubRepo) ([]string, error) {
	if m.listTagsFunc != nil {
		return m.listTagsFunc(ctx, repo)
	}
	return []string{}, nil
}

func (m *branchCreatorMock) CreateAnnotatedTag(ctx context.Context, repo GithubRepo, tag string, commit string, message string) error {
	if m.createTagFunc != nil {
		return m.createTagFunc(ctx, repo, tag, commit, message)
	}
	return nil
}

func (m *branchCreatorMock) GetBranchesForCommit(ctx context.Context, repo GithubRepo, commit string) ([]string, error) {
	if m.getBranchesFunc != nil {
		return m.getBranchesFunc(ctx, repo, commit)
	}
	return []string{}, nil
}

func TestRepoService_CreateTidbXHotfixTag(t *testing.T) {
	t.Run("with commit only", func(t *testing.T) {
		mock := &branchCreatorMock{
			getSHAFunc: func(ctx context.Context, repo GithubRepo, ref string) (string, error) {
				return "abc123def456", nil
			},
			listTagsFunc: func(ctx context.Context, repo GithubRepo) ([]string, error) {
				return []string{"v8.5.4-nextgen.202510.10"}, nil
			},
		}
		s := repoService{
			now: func() time.Time {
				return time.Date(2025, 10, 15, 0, 0, 0, 0, time.UTC)
			},
			branchCreator: mock,
		}
		
		resp, err := s.CreateTidbXHotfixTag(context.TODO(), TidbXHotfixTagCreateReq{
			Repo:   "pingcap/tidb",
			Commit: "abc123",
			Author: "test-user",
		})
		
		assert.NoError(t, err)
		assert.Equal(t, "pingcap/tidb", resp.Repo)
		assert.Equal(t, "abc123def456", resp.Commit)
		assert.Equal(t, "v8.5.4-nextgen.202510.11", resp.Tag)
	})

	t.Run("with branch only", func(t *testing.T) {
		mock := &branchCreatorMock{
			getSHAFunc: func(ctx context.Context, repo GithubRepo, ref string) (string, error) {
				return "def456abc789", nil
			},
			listTagsFunc: func(ctx context.Context, repo GithubRepo) ([]string, error) {
				return []string{}, nil
			},
		}
		s := repoService{
			now: func() time.Time {
				return time.Date(2025, 11, 1, 0, 0, 0, 0, time.UTC)
			},
			branchCreator: mock,
		}
		
		resp, err := s.CreateTidbXHotfixTag(context.TODO(), TidbXHotfixTagCreateReq{
			Repo:   "pingcap/tidb",
			Branch: "release-8.5",
			Author: "test-user",
		})
		
		assert.NoError(t, err)
		assert.Equal(t, "pingcap/tidb", resp.Repo)
		assert.Equal(t, "def456abc789", resp.Commit)
		assert.Equal(t, "v8.5.4-nextgen.202511.1", resp.Tag)
	})

	t.Run("with both branch and commit matching", func(t *testing.T) {
		mock := &branchCreatorMock{
			getSHAFunc: func(ctx context.Context, repo GithubRepo, ref string) (string, error) {
				return "same123sha456", nil
			},
			listTagsFunc: func(ctx context.Context, repo GithubRepo) ([]string, error) {
				return []string{"v8.5.4-nextgen.202510.5"}, nil
			},
		}
		s := repoService{
			now: func() time.Time {
				return time.Date(2025, 10, 1, 0, 0, 0, 0, time.UTC)
			},
			branchCreator: mock,
		}
		
		resp, err := s.CreateTidbXHotfixTag(context.TODO(), TidbXHotfixTagCreateReq{
			Repo:   "pingcap/tidb",
			Branch: "release-8.5",
			Commit: "same123",
			Author: "test-user",
		})
		
		assert.NoError(t, err)
		assert.Equal(t, "v8.5.4-nextgen.202510.6", resp.Tag)
	})

	t.Run("with both branch and commit - commit in branch", func(t *testing.T) {
		mock := &branchCreatorMock{
			getSHAFunc: func(ctx context.Context, repo GithubRepo, ref string) (string, error) {
				if ref == "release-8.5" {
					return "branch123sha", nil
				}
				return "commit123sha", nil
			},
			listTagsFunc: func(ctx context.Context, repo GithubRepo) ([]string, error) {
				return []string{}, nil
			},
			getBranchesFunc: func(ctx context.Context, repo GithubRepo, commit string) ([]string, error) {
				return []string{"release-8.5", "main"}, nil
			},
		}
		s := repoService{
			now: func() time.Time {
				return time.Date(2025, 10, 1, 0, 0, 0, 0, time.UTC)
			},
			branchCreator: mock,
		}
		
		resp, err := s.CreateTidbXHotfixTag(context.TODO(), TidbXHotfixTagCreateReq{
			Repo:   "pingcap/tidb",
			Branch: "release-8.5",
			Commit: "commit123",
			Author: "test-user",
		})
		
		assert.NoError(t, err)
		assert.Equal(t, "commit123sha", resp.Commit)
	})

	t.Run("error when neither branch nor commit provided", func(t *testing.T) {
		s := repoService{
			now:           time.Now,
			branchCreator: &branchCreatorMock{},
		}
		
		_, err := s.CreateTidbXHotfixTag(context.TODO(), TidbXHotfixTagCreateReq{
			Repo:   "pingcap/tidb",
			Author: "test-user",
		})
		
		assert.Error(t, err)
		assert.Contains(t, err.Error(), "at least one of 'branch' or 'commit' must be provided")
	})

	t.Run("error when commit not in branch", func(t *testing.T) {
		mock := &branchCreatorMock{
			getSHAFunc: func(ctx context.Context, repo GithubRepo, ref string) (string, error) {
				if ref == "release-8.5" {
					return "branch123sha", nil
				}
				return "commit456sha", nil
			},
			getBranchesFunc: func(ctx context.Context, repo GithubRepo, commit string) ([]string, error) {
				return []string{"main", "develop"}, nil
			},
		}
		s := repoService{
			now:           time.Now,
			branchCreator: mock,
		}
		
		_, err := s.CreateTidbXHotfixTag(context.TODO(), TidbXHotfixTagCreateReq{
			Repo:   "pingcap/tidb",
			Branch: "release-8.5",
			Commit: "commit456",
			Author: "test-user",
		})
		
		assert.Error(t, err)
		assert.Contains(t, err.Error(), "does not exist in branch")
	})

	t.Run("error with invalid repo format", func(t *testing.T) {
		s := repoService{
			now:           time.Now,
			branchCreator: &branchCreatorMock{},
		}
		
		_, err := s.CreateTidbXHotfixTag(context.TODO(), TidbXHotfixTagCreateReq{
			Repo:   "invalid-repo-format",
			Commit: "abc123",
			Author: "test-user",
		})
		
		assert.Error(t, err)
		assert.Contains(t, err.Error(), "invalid repo format")
	})
}
