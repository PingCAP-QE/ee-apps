package github

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
)

type RepoInfo struct {
	Name     string   `json:"name"`
	FullName string   `json:"full_name"`
	Private  bool     `json:"private"`
	Owner    UserInfo `json:"owner"`
	HtmlUrl  string   `json:"html_url"`
	Fork     bool     `json:"fork"`
}

type PrRepo struct {
	Label string   `json:"label"`
	Ref   string   `json:"ref"`
	Sha   string   `json:"sha"`
	User  UserInfo `json:"user"`
	Repo  RepoInfo `json:"repo"`
}

type PRInfo struct {
	URL               string      `json:"url"`
	Id                int         `json:"id"`
	NodeId            string      `json:"node_id"`
	HtmlUrl           string      `json:"html_url"`
	DiffUrl           string      `json:"diff_url"`
	PatchUrl          string      `json:"patch_url"`
	IssueUrl          string      `json:"issue_url"`
	Number            string      `json:"number"`
	State             string      `json:"state"`
	Locked            string      `json:"locked"`
	Title             string      `json:"title"`
	User              UserInfo    `json:"user"`
	Body              string      `json:"body"`
	CreateAt          string      `json:"created_at"`
	UpdateAt          string      `json:"updated_at"`
	CloseAt           string      `json:"closed_at"`
	MergedAt          string      `json:"merged_at"`
	MergeCommit       string      `json:"merge_commit_sha"`
	Assignee          string      `json:"assignee"`
	Labels            []labelInfo `json:"labels"`
	CommitsUrl        string      `json:"commits_url"`
	ReviewCommentsUrl string      `json:"review_comments_url"`
	CommentsUrl       string      `json:"comments_url"`
	Head              PrRepo      `json:"head"`
	Base              PrRepo      `json:"base"`
}

// state : open closed all
func (r *Repo) GetPRList(page int, state string) ([]PRInfo, error) {
	apiUrl := apiURL + fmt.Sprintf("%s/%s/pulls?page=%d&state=%s", r.Org, r.Repo, page, state)

	resp, err := apiCall(apiUrl)
	if err != nil {
		log.Printf("request error : %s \n", err.Error())
		return nil, err

	}

	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Printf("get response error : %s \n", err.Error())
		return nil, err
	}

	var result []PRInfo

	if err := json.Unmarshal(body, &result); err != nil {
		return nil, err
	}
	return result, nil
}

// filter base branch
func (r *Repo) GetPRListByBase(page int, state string, base string) ([]PRInfo, error) {
	apiUrl := apiURL + fmt.Sprintf("%s/%s/pulls?page=%d&state=%s&base=%s", r.Org, r.Repo, page, state, base)

	resp, err := apiCall(apiUrl)
	if err != nil {
		log.Printf("request error : %s \n", err.Error())
		return nil, err

	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Printf("get response error : %s \n", err.Error())
		return nil, err
	}

	var result []PRInfo
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, err
	}

	//fmt.Printf("Result : %+v\n", result)
	return result, nil
}

// state : open closed all
func (r *Repo) GetPRMergedCIStatus(page int, state string) ([]PRInfo, error) {
	prList, err := r.GetPRList(page, state)
	if err != nil {
		log.Println("get pr list failed!")
		return nil, err
	}
	_ = prList
	return prList, nil
}
