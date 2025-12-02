package repo

import (
	"context"
	"fmt"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

type ChatPrinter struct {
}

var _ service.RepoHeadCreator = ChatPrinter{}

func (p ChatPrinter) CreateBranchFromTag(ctx context.Context, repo service.GithubRepo, branch string, tag string) error {
	fmt.Println(GenEEChatOpsCreateBranch(repo, branch, tag))
	return nil
}

func (p ChatPrinter) CreateTagFromBranch(ctx context.Context, repo service.GithubRepo, tag string, branch string) error {
	fmt.Println(GenEEChatOpsCreateTag(repo, tag, branch))
	return nil
}

func (p ChatPrinter) GetCommitSHA(ctx context.Context, repo service.GithubRepo, ref string) (string, error) {
	return "", nil
}

func (p ChatPrinter) ListTags(ctx context.Context, repo service.GithubRepo) ([]string, error) {
	return []string{}, nil
}

func (p ChatPrinter) CreateAnnotatedTag(ctx context.Context, repo service.GithubRepo, tag string, commit string, message string) error {
	return nil
}

func (p ChatPrinter) GetBranchesForCommit(ctx context.Context, repo service.GithubRepo, commit string) ([]string, error) {
	return []string{}, nil
}

func GenEEChatOpsCreateBranch(repo service.GithubRepo, branch string, tag string) string {
	return fmt.Sprintf(" /create_branch_from_tag %s %s", service.GenTagURL(repo.URL(), tag), branch)
}

func GenEEChatOpsCreateTag(repo service.GithubRepo, tag string, branch string) string {
	return fmt.Sprintf(" /create_tag_from_branch %s %s", service.GenBranchURL(repo.URL(), branch), tag)
}
