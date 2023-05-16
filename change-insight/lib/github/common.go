package github

import (
	"log"
	"net/http"
)

type Repo struct {
	Org  string // eg: pingcap
	Repo string // eg: tidb
}
type UserInfo struct {
	Login     string `json:"login"`
	Id        int    `json:"id"`
	Url       string `json:"url"`
	SiteAdmin string `json:"site_admin"`
}

type labelInfo struct {
	Id          int    `json:"id"`
	NodeId      string `json:"node_id"`
	Url         string `json:"url"`
	Name        string `json:"name"`
	Default     bool   `json:"default"`
	Description string `json:"description"`
}

var token string = "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"
var apiURL string = "https://api.github.com/repos/"

func apiCall(apiUrl string) (*http.Response, error) {
	req, err := http.NewRequest("GET", apiUrl, nil)
	if err != nil {
		log.Printf("request init error : %s \n", err.Error())
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Authorization", "token "+token)
	//log.Printf("req: %+v \n", req)
	return http.DefaultClient.Do(req)
}
