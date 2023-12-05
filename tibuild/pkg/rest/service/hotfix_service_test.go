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
