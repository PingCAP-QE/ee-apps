package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/jmoiron/sqlx"

	_ "github.com/go-sql-driver/mysql"
)

const (
	SQL_TABLE_DSL = `CREATE TABLE IF NOT EXISTS tiinsight_problem_case_runs (
  id INT auto_increment,
  repo VARCHAR(255), -- repo full name
  branch VARCHAR(255), -- base branch
  suite_name VARCHAR(255), -- suite name, target name in bazel.
  case_name VARCHAR(255), -- case name, may be TextXxx.TestYyy format.
  report_time timestamp, -- unit timestamp
  flaky BIT, -- true or false
  timecost_ms INT, -- milliseconds.
  build_url VARCHAR(1024), -- CI build url
  primary key (id)
);
`
	SQL_TPL_INSERT = `INSERT INTO tiinsight_problem_case_runs(
			repo, 
			branch, 
			suite_name, 
			case_name, 
			report_time, 
			flaky, 
			timecost_ms, 
			build_url
		) values (
			:repo, 
			:branch, 
			:suite_name, 
			:case_name, 
			:report_time, 
			:flaky, 
			:timecost_ms, 
			:build_url
		);`
)

type CliParams struct {
	DSN         string `flag:"dsn"`
	Repo        string `flag:"repo"`
	Branch      string `flag:"branch"`
	CaseDataURL string `flag:"caseDataURL"`
	BuildURL    string `flag:"build_url"`
}

type ProblemCasesFromBazel struct {
	NewFlaky []string           `json:"new_flaky"`
	LongTime map[string]float64 `json:"long_time"`
}

func ReceiveHandler(event cloudevents.Event) cloudevents.Result {
	caseData := make(map[string]ProblemCasesFromBazel)
	if err := event.DataAs(&caseData); err != nil {
		return cloudevents.NewHTTPResult(http.StatusBadRequest, err.Error())
	}

	buildURL := event.Source()

	params := parseCLIParams()

	// Parse CLI flags if provided

	caseData, err := readCaseDataFromURL(params.CaseDataURL)
	if err != nil {
		log.Fatal(err)
	}

	db, err := sqlx.Connect("mysql", params.DSN)
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	// Create table if not exists
	_, err = db.Exec(SQL_TABLE_DSL)
	if err != nil {
		log.Fatal(err)
	}

	// Insert records
	insertProblemCaseRuns(db, caseData, params.Repo, params.Branch, time.Now().Unix(), params.BuildURL)

	fmt.Println("Data inserted successfully!")
}

func parseCLIParams() *CliParams {
	var ret CliParams

	flag.StringVar(&ret.DSN, "dsn", "localhost:3306", "data source name")
	flag.StringVar(&ret.Repo, "repo", "localhost:3306", "data source name")
	flag.StringVar(&ret.Branch, "branch", "localhost:3306", "data source name")
	flag.StringVar(&ret.CaseDataURL, "case_data_url", "localhost:3306", "data source name")
	flag.StringVar(&ret.BuildURL, "build_url", "localhost:3306", "data source name")

	flag.Parse()

	return &ret
}

func readCaseDataFromURL(url string) (map[string]ProblemCasesFromBazel, error) {
	// Send GET request to the URL
	resp, err := http.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	// Read the response body
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	caseData := make(map[string]ProblemCasesFromBazel)
	json.Unmarshal(body, &caseData)

	return caseData, err
}

func insertProblemCaseRuns(db *sqlx.DB, caseData map[string]ProblemCasesFromBazel, repo, branch string, timestamp int64, buildURL string) {
	for target, caseResults := range caseData {
		for _, flakyCase := range caseResults.NewFlaky {
			_, err := db.NamedExec(SQL_TPL_INSERT, map[string]interface{}{
				"repo":        repo,
				"branch":      branch,
				"suite_name":  target,
				"case_name":   flakyCase,
				"report_time": time.Unix(timestamp, 0),
				"flaky":       true,
				"timecost_ms": 0,
				"build_url":   buildURL,
			})
			if err != nil {
				log.Fatal(err)
			}
		}

		for tc, timecost := range caseResults.LongTime {
			_, err := db.NamedExec(SQL_TPL_INSERT, map[string]interface{}{
				"repo":        repo,
				"branch":      branch,
				"suite_name":  target,
				"case_name":   tc,
				"report_time": time.Unix(timestamp, 0),
				"flaky":       false,
				"timecost_ms": int(timecost * 1000),
				"build_url":   buildURL,
			})
			if err != nil {
				log.Fatal(err)
			}
		}
	}
}
