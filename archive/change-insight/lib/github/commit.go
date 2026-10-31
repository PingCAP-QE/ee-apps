package github

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
)

type CommitStatus struct {
	State      string             `json:"state"`
	Statuses   []CommitStatusInfo `json:"statuses"`
	Sha        string             `json:"sha"`
	TotalCount int                `json:"total_count"`
	CommitURL  string             `json:"commit_url"`
	URL        string             `json:"url"`
}

type CommitStatusInfo struct {
	Url         string
	AvatarUrl   string `json:"avatar_url"`
	Id          int    `json:"id"`
	NodeId      string `json:"node_id"`
	State       string `json:"state"`
	Description string `json:"description"`
	TargetUrl   string `json:"target_url"`
	Context     string `json:"context"`
	CreateAt    string `json:"created_at"`
	UpdateAt    string `json:"updated_at"`
}

func (r *Repo) GetCommitStatus(commitID string) (*CommitStatus, error) {
	apiUrl := apiURL + fmt.Sprintf("%s/%s/commits/%s/status", r.Org, r.Repo, commitID)

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

	var result CommitStatus
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, err
	}

	return &result, nil
}
