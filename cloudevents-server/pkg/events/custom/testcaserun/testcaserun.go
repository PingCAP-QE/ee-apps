// Package testcaserun implement handlers for test case run report events.
package testcaserun

import (
	"context"
	"net/http"
	"time"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/ent"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/types"
)

type Handler struct {
	Storage *ent.ProblemCaseRunClient
}

// Handle for test case run events
func (h *Handler) Handle(event cloudevents.Event) cloudevents.Result {
	caseData := make(map[string]ProblemCasesFromBazel)
	if err := event.DataAs(&caseData); err != nil {
		return cloudevents.NewHTTPResult(http.StatusBadRequest, err.Error())
	}

	buildURL, _ := types.ToString(event.Extensions()["buildurl"])
	repo, _ := types.ToString(event.Extensions()["repo"])
	branch, _ := types.ToString(event.Extensions()["branch"])

	// Insert records
	if err := h.addRecords(context.Background(), caseData, repo, branch, buildURL); err != nil {
		return cloudevents.NewHTTPResult(http.StatusInternalServerError, "insert failed: %v", err)
	}

	return cloudevents.ResultACK
}

func (h *Handler) addRecords(ctx context.Context, caseData map[string]ProblemCasesFromBazel, repo, branch, buildURL string) error {
	reportTime := time.Now()

	var recordBuilders []*ent.ProblemCaseRunCreate
	for target, caseResults := range caseData {
		for _, tc := range caseResults.NewFlaky {
			recordBuilders = append(recordBuilders,
				h.Storage.Create().
					SetRepo(repo).
					SetBranch(branch).
					SetSuiteName(target).
					SetCaseName(tc.Name).
					SetBuildURL(buildURL).
					SetTimecostMs(0).
					SetReportTime(reportTime).
					SetFlaky(true).
					SetReason(tc.Reason),
			)
		}

		for tc, timecost := range caseResults.LongTime {
			entry := h.Storage.Create().
				SetRepo(repo).
				SetBranch(branch).
				SetSuiteName(target).
				SetCaseName(tc).
				SetBuildURL(buildURL).
				SetTimecostMs(int(timecost * 1000)).
				SetReportTime(reportTime).
				SetFlaky(false)
			if timecost < 0 {
				entry.SetReason(reasonNotFinished)
			}

			recordBuilders = append(recordBuilders, entry)
		}
	}

	return h.Storage.CreateBulk(recordBuilders...).Exec(ctx)
}
