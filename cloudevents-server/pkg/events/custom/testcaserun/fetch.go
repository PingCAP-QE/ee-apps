package testcaserun

import (
	"encoding/json"
	"io"
	"net/http"
)

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
