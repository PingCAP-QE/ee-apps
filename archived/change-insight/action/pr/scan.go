package pr

import (
	"log"

	"github.com/PingCAP-QE/ee-apps/change-insight/lib/DB"
	"github.com/PingCAP-QE/ee-apps/change-insight/lib/github"
)

// Org : pingcap  Repo: tidb  status: all
func ScanPR(org string, repo string, status string) []github.PRInfo {
	repoObject := &github.Repo{Org: org, Repo: repo}
	//maxPage := 1
	PRRestInfo := make([]github.PRInfo, 0)
	maxPage := 100000
	for i := 0; i < maxPage; i++ {
		PRInfo, err := repoObject.GetPRList(i, status)
		if err != nil {
			log.Println("Get PR Error:", err.Error())
			return PRRestInfo
		}
		for _, pr := range PRInfo {
			log.Println(pr)
		}
		PRRestInfo = append(PRRestInfo, PRInfo...)
	}
	return PRRestInfo
}

// TODO : complete the function
func ChangePr4DBStruct(prinfolist []github.PRInfo) []DB.PRInfo {
	for _, prfullinfo := range prinfolist {
		_ = prfullinfo
	}
	return nil
}

func ScanSave(org string, repo string, status string) {
	prfullInfolist := ScanPR(org, repo, status)
	prlist := ChangePr4DBStruct(prfullInfolist)
	DB.InsertPRInfo(prlist)
}
