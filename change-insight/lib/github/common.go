package github

import (
	"log"
	"net/http"
	"os"
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

type Config struct {
	Token  string
	ApiURL string
}

func NewConfig() (*Config, error) {
	token := os.Getenv("GITHUB_TOKEN")
	if token == "" {
		return nil, fmt.Errorf("GITHUB_TOKEN environment variable is not set")
	}
	return &Config{
		Token:  token,
		ApiURL: "https://api.github.com/repos/",
	}, nil
}

func apiCall(config *Config, apiUrl string) (*http.Response, error) {
	req, err := http.NewRequest("GET", apiUrl, nil)
	if err != nil {
		log.Printf("request init error: %s\n", err.Error())
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Authorization", "token "+config.Token)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Printf("request error: %s\n", err.Error())
		return nil, err
	}

	if resp.StatusCode != http.StatusOK {
		log.Printf("unexpected status code: %d\n", resp.StatusCode)
		return resp, fmt.Errorf("unexpected status code: %d", resp.StatusCode)
	}

	return resp, nil
}
