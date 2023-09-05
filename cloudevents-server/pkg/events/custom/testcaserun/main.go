package testcaserun

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"time"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/types"

	_ "github.com/go-sql-driver/mysql"
	_ "github.com/mattn/go-sqlite3"
)

type ProblemCasesFromBazel struct {
	NewFlaky []string           `yaml:"new_flaky,omitempty" json:"new_flaky,omitempty"`
	LongTime map[string]float64 `yaml:"long_time,omitempty" json:"long_time,omitempty"`
}

func ReceiveHandler(event cloudevents.Event) cloudevents.Result {
	caseData := make(map[string]ProblemCasesFromBazel)
	if err := event.DataAs(&caseData); err != nil {
		return cloudevents.NewHTTPResult(http.StatusBadRequest, err.Error())
	}

	buildURL, _ := types.ToString(event.Extensions()["build_url"])
	repo, _ := types.ToString(event.Extensions()["repo"])
	branch, _ := types.ToString(event.Extensions()["branch"])

	db, err := ent.Open("sqlite3", "file:ent.sqlite3?mode=memory&cache=shared&_fk=1")
	if err != nil {
		log.Fatalf("failed opening connection to sqlite: %v", err)
	}
	defer db.Close()
	// Run the auto migration tool.
	if err := db.Schema.Create(context.Background()); err != nil {
		log.Fatalf("failed creating schema resources: %v", err)
	}

	// Insert records
	if err := insertProblemCaseRuns(context.Background(), db, caseData, repo, branch, buildURL); err != nil {
		return cloudevents.NewReceipt(true, "insert failed")
	}

	return cloudevents.ResultACK
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

func insertProblemCaseRuns(ctx context.Context, db *ent.Client, caseData map[string]ProblemCasesFromBazel, repo, branch, buildURL string) error {
	reportTime := time.Now()

	var recordBuilders []*ent.ProblemCaseRunCreate
	for target, caseResults := range caseData {
		for _, tc := range caseResults.NewFlaky {
			recordBuilders = append(recordBuilders,
				db.ProblemCaseRun.Create().
					SetRepo(repo).
					SetBranch(branch).
					SetSuiteName(target).
					SetCaseName(tc).
					SetBuildURL(buildURL).
					SetTimecostMs(0).
					SetReportTime(reportTime).
					SetFlaky(true),
			)
		}

		for tc, timecost := range caseResults.LongTime {
			recordBuilders = append(recordBuilders,
				db.ProblemCaseRun.Create().
					SetRepo(repo).
					SetBranch(branch).
					SetSuiteName(target).
					SetCaseName(tc).
					SetBuildURL(buildURL).
					SetTimecostMs(int(timecost*1000)).
					SetReportTime(reportTime).
					SetFlaky(false),
			)
		}
	}

	return db.ProblemCaseRun.CreateBulk(recordBuilders...).Exec(ctx)
}
