package repo

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"tibuild/pkg/rest/service"
)

type httpDoer interface {
	Do(req *http.Request) (*http.Response, error)
}

type githubHeadCreator struct {
	Token    string
	httpDoer httpDoer
}

var _ service.RepoHeadCreator = githubHeadCreator{}

func (g githubHeadCreator) CreateBranchFromTag(ctx context.Context, repo service.GithubRepo, branch string, tag string) error {
	sha, err := g.getTagHash(ctx, repo, tag)
	if err != nil {
		return err
	}
	return g.createBranch(ctx, repo, branch, sha)
}

func (g githubHeadCreator) getTagHash(ctx context.Context, repo service.GithubRepo, tag string) (string, error) {
	url := fmt.Sprintf("https://api.github.com/repos/%s/%s/git/refs/tags/%s", repo.Owner, repo.Repo, tag)
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "token "+g.Token)
	resp, err := g.httpDoer.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("bad http status: %s", req.URL.String())
	}
	rt := struct {
		Object struct {
			Sha string `json:"sha"`
		} `json:"object"`
	}{}
	err = json.NewDecoder(resp.Body).Decode(&rt)
	if err != nil {
		return "", err
	}
	return rt.Object.Sha, nil
}

const HeaderAuthorization = "Authorization"

func (g githubHeadCreator) createBranch(ctx context.Context, repo service.GithubRepo, branch string, commitHash string) error {
	url := fmt.Sprintf("https://api.github.com/repos/%s/%s/git/refs", repo.Owner, repo.Repo)
	body := map[string]string{
		"ref": "refs/heads/" + branch,
		"sha": commitHash,
	}
	buffer := &bytes.Buffer{}
	enc := json.NewEncoder(buffer)
	err := enc.Encode(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequest("POST", url, buffer)
	if err != nil {
		return err
	}
	req.Header.Set(HeaderAuthorization, "token "+g.Token)
	resp, err := g.httpDoer.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == 422 {
		return fmt.Errorf("github refuse to create branch%w", service.ErrServerRefuse)
	} else if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		all, err := io.ReadAll(resp.Body)
		if err != nil {
			return err
		}
		fmt.Println(string(all))
		return fmt.Errorf("bad http status: %s", req.URL.String())
	}
	return nil
}

func (g githubHeadCreator) CreateTagFromBranch(ctx context.Context, repo service.GithubRepo, tag string, branch string) error {
	//TODO implement me
	panic("implement me")
}

func NewGithubHeadCreator(token string) githubHeadCreator {
	return githubHeadCreator{Token: token, httpDoer: http.DefaultClient}
}
