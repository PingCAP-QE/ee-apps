package service

import (
	"context"
	"os"
	"reflect"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestGetHash(t *testing.T) {
	if os.Getenv("TEST_FANOUT") == "" {
		t.Skip("Skipping send event")
	}
	token := os.Getenv("GHTOKEN")
	hash, err := NewGHClient(token).GetHash(context.TODO(), RepoPd.Owner, RepoPd.Repo, "branch/master")
	require.NoError(t, err)
	require.NotEmpty(t, hash)
}

func TestGetHashSha1(t *testing.T) {
	s := "754095a9f460dcf31f053045cfedfb00b9ad8e81"
	hash, err := NewGHClient("").GetHash(context.TODO(), RepoPd.Owner, RepoPd.Repo, s)
	require.NoError(t, err)
	require.Equal(t, s, hash)
}

func TestGitHubClient_GetBranchesForCommit(t *testing.T) {
	type args struct {
		owner  string
		repo   string
		commit string
	}
	tests := []struct {
		name    string
		args    args
		want    []string
		wantErr bool
	}{
		{
			name: "test",
			args: args{
				owner:  "pingcap",
				repo:   "tidb",
				commit: "0ccee0e011054ebfc86a6d5faa59a689a0793c05",
			},
			want:    []string{"master"},
			wantErr: false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c := NewGHClient("")
			got, err := c.GetBranchesForCommit(context.TODO(), tt.args.owner, tt.args.repo, tt.args.commit)
			if (err != nil) != tt.wantErr {
				t.Errorf("GitHubClient.GetBranchesForCommit() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("GitHubClient.GetBranchesForCommit() = %v, want %v", got, tt.want)
			}
		})
	}
}
