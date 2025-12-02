package repo

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
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

func (g githubHeadCreator) GetCommitSHA(ctx context.Context, repo service.GithubRepo, ref string) (string, error) {
	url := fmt.Sprintf("https://api.github.com/repos/%s/%s/commits/%s", repo.Owner, repo.Repo, ref)
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set(HeaderAuthorization, "token "+g.Token)
	resp, err := g.httpDoer.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("failed to get commit SHA (status %d): %s", resp.StatusCode, string(body))
	}
	rt := struct {
		Sha string `json:"sha"`
	}{}
	err = json.NewDecoder(resp.Body).Decode(&rt)
	if err != nil {
		return "", err
	}
	return rt.Sha, nil
}

func (g githubHeadCreator) ListTags(ctx context.Context, repo service.GithubRepo) ([]string, error) {
	var allTags []string
	page := 1
	perPage := 100
	
	for {
		url := fmt.Sprintf("https://api.github.com/repos/%s/%s/tags?page=%d&per_page=%d", repo.Owner, repo.Repo, page, perPage)
		req, err := http.NewRequest("GET", url, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set(HeaderAuthorization, "token "+g.Token)
		resp, err := g.httpDoer.Do(req)
		if err != nil {
			return nil, err
		}
		
		if resp.StatusCode != http.StatusOK {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			return nil, fmt.Errorf("failed to list tags (status %d): %s", resp.StatusCode, string(body))
		}
		
		var tags []struct {
			Name string `json:"name"`
		}
		err = json.NewDecoder(resp.Body).Decode(&tags)
		resp.Body.Close()
		if err != nil {
			return nil, err
		}
		
		if len(tags) == 0 {
			break
		}
		
		for _, tag := range tags {
			allTags = append(allTags, tag.Name)
		}
		
		if len(tags) < perPage {
			break
		}
		page++
	}
	
	return allTags, nil
}

func (g githubHeadCreator) CreateAnnotatedTag(ctx context.Context, repo service.GithubRepo, tag string, commit string, message string) error {
	// First, create a tag object
	tagObjectURL := fmt.Sprintf("https://api.github.com/repos/%s/%s/git/tags", repo.Owner, repo.Repo)
	tagBody := map[string]interface{}{
		"tag":     tag,
		"message": message,
		"object":  commit,
		"type":    "commit",
	}
	
	buffer := &bytes.Buffer{}
	err := json.NewEncoder(buffer).Encode(tagBody)
	if err != nil {
		return err
	}
	
	req, err := http.NewRequest("POST", tagObjectURL, buffer)
	if err != nil {
		return err
	}
	req.Header.Set(HeaderAuthorization, "token "+g.Token)
	resp, err := g.httpDoer.Do(req)
	if err != nil {
		return err
	}
	
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return fmt.Errorf("failed to create tag object (status %d): %s", resp.StatusCode, string(body))
	}
	
	var tagResp struct {
		Sha string `json:"sha"`
	}
	err = json.NewDecoder(resp.Body).Decode(&tagResp)
	resp.Body.Close()
	if err != nil {
		return err
	}
	
	// Now create the reference
	refURL := fmt.Sprintf("https://api.github.com/repos/%s/%s/git/refs", repo.Owner, repo.Repo)
	refBody := map[string]string{
		"ref": "refs/tags/" + tag,
		"sha": tagResp.Sha,
	}
	
	buffer = &bytes.Buffer{}
	err = json.NewEncoder(buffer).Encode(refBody)
	if err != nil {
		return err
	}
	
	req, err = http.NewRequest("POST", refURL, buffer)
	if err != nil {
		return err
	}
	req.Header.Set(HeaderAuthorization, "token "+g.Token)
	resp, err = g.httpDoer.Do(req)
	if err != nil {
		return err
	}
	
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return fmt.Errorf("failed to create tag reference (status %d): %s", resp.StatusCode, string(body))
	}
	resp.Body.Close()
	
	return nil
}

func (g githubHeadCreator) GetBranchesForCommit(ctx context.Context, repo service.GithubRepo, commit string) ([]string, error) {
	// Use the list branches endpoint to get all branches, then check if commit is in each branch
	// This is more reliable than the deprecated branches-where-head endpoint
	url := fmt.Sprintf("https://api.github.com/repos/%s/%s/branches", repo.Owner, repo.Repo)
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set(HeaderAuthorization, "token "+g.Token)
	resp, err := g.httpDoer.Do(req)
	if err != nil {
		return nil, err
	}
	
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return nil, fmt.Errorf("failed to get branches (status %d): %s", resp.StatusCode, string(body))
	}
	
	var branches []struct {
		Name   string `json:"name"`
		Commit struct {
			Sha string `json:"sha"`
		} `json:"commit"`
	}
	err = json.NewDecoder(resp.Body).Decode(&branches)
	resp.Body.Close()
	if err != nil {
		return nil, err
	}
	
	var branchNames []string
	for _, b := range branches {
		// Check if the commit is reachable from this branch by comparing
		compareURL := fmt.Sprintf("https://api.github.com/repos/%s/%s/compare/%s...%s", 
			repo.Owner, repo.Repo, commit, b.Name)
		compareReq, err := http.NewRequest("GET", compareURL, nil)
		if err != nil {
			continue
		}
		compareReq.Header.Set(HeaderAuthorization, "token "+g.Token)
		compareResp, err := g.httpDoer.Do(compareReq)
		if err != nil {
			continue
		}
		
		if compareResp.StatusCode == http.StatusOK {
			var compareResult struct {
				Status string `json:"status"`
			}
			err = json.NewDecoder(compareResp.Body).Decode(&compareResult)
			compareResp.Body.Close()
			if err == nil && (compareResult.Status == "identical" || compareResult.Status == "behind") {
				// Commit is in this branch
				branchNames = append(branchNames, b.Name)
			}
		} else {
			compareResp.Body.Close()
		}
	}
	
	return branchNames, nil
}

func NewGithubHeadCreator(token string) githubHeadCreator {
	return githubHeadCreator{Token: token, httpDoer: http.DefaultClient}
}
