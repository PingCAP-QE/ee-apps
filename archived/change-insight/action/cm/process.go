package cm

import (
	"log"
	"strings"

	"github.com/PingCAP-QE/ee-apps/change-insight/lib/gitopr"
)

// var WorkSpace = "~" //将代码库下载到本地的目录，方便后面分析
var WorkSpace string //将代码库下载到本地的目录，方便后面分析

// 将返回的 FileInfo 绑定这个 File 改动的 commit 信息

// Variable ==> list ==>
type CMFileOperStuctInfo struct {
	RepoUrl  string
	Product  string
	FileOper map[string][]gitopr.Commit // filename ==> file oper commitIfno
}

type safeMap struct {
	CMFileOperInfo map[string][]CMFileOperStuctInfo
}

// CMInfoByDurationTime 返回最终数据格式 前端进行渲染
func CMInfoByDurationTime(beginDate string, endData string) map[string][]CMFileOperStuctInfo {
	CMResultInfo := safeMap{}
	CMResultInfo.CMFileOperInfo = make(map[string][]CMFileOperStuctInfo)
	ConfigInfo := GetConfigValue()
	for configType, typeRangeList := range ConfigInfo {
		CMFileOperInfoList := []CMFileOperStuctInfo{} // 一个 configType 对应多个 repo 下的 多个fileList （[]CMFileOperInfo）
		for _, typeRepoInfo := range typeRangeList {
			CMFileOperInfo := CMFileOperStuctInfo{}
			CMFileOperInfo.Product = typeRepoInfo.Product
			CMFileOperInfo.RepoUrl = typeRepoInfo.RepoUrl
			CMFileOperInfo.FileOper = make(map[string][]gitopr.Commit)
			// 填充每个文件变更的 commit 信息
			org := strings.Split(typeRepoInfo.RepoUrl, "/")[0]
			repo := strings.Join(strings.Split(typeRepoInfo.RepoUrl, "/")[1:], "/")
			repo = strings.Replace(repo, ".git$", "", -1)
			gitObject := &gitopr.GitObject{
				WS:     WorkSpace,
				Org:    org,
				Repo:   repo,
				Branch: "master", //默认拉的分支，先这么写 后面如果有 master 和 main 再进行逻辑区分
			}
			// 每一个文件的变更详细内容
			for _, fileName := range typeRepoInfo.FileList {
				commitList, err := gitObject.CommitInfomationByDate(beginDate, endData, fileName)
				if err != nil {
					log.Printf("get commit info in the file[%s] failed: [%s] \n", fileName, err.Error())
					continue
				}
				CMFileOperInfo.FileOper[fileName] = commitList
				//CMResultInfo[configType] = append(CMResultInfo[configType])

			}
			CMFileOperInfoList = append(CMFileOperInfoList, CMFileOperInfo)
		}
		CMResultInfo.CMFileOperInfo[configType] = CMFileOperInfoList
	}
	return CMResultInfo.CMFileOperInfo
}

// CMInfoByDurationTime 返回最终数据格式 前端进行渲染
func CMInfoByBranch(releaseBranch1 string, releaseBranch2 string) map[string][]CMFileOperStuctInfo {
	CMResultInfo := safeMap{}
	CMResultInfo.CMFileOperInfo = make(map[string][]CMFileOperStuctInfo)
	ConfigInfo := GetConfigValue()
	for configType, typeRangeList := range ConfigInfo {
		CMFileOperInfoList := []CMFileOperStuctInfo{} // 一个 configType 对应多个 repo 下的 多个fileList （[]CMFileOperInfo）
		for _, typeRepoInfo := range typeRangeList {
			CMFileOperInfo := CMFileOperStuctInfo{}
			CMFileOperInfo.Product = typeRepoInfo.Product
			CMFileOperInfo.RepoUrl = typeRepoInfo.RepoUrl
			CMFileOperInfo.FileOper = make(map[string][]gitopr.Commit)
			// 填充每个文件变更的 commit 信息
			org := strings.Split(typeRepoInfo.RepoUrl, "/")[0]
			repo := strings.Join(strings.Split(typeRepoInfo.RepoUrl, "/")[1:], "/")
			repo = strings.Replace(repo, ".git$", "", -1)
			gitObject := &gitopr.GitObject{
				WS:     WorkSpace,
				Org:    org,
				Repo:   repo,
				Branch: "master", //默认拉的分支，先这么写 后面如果有 master 和 main 再进行逻辑区分
			}
			// TODO move CT do these step daily
			//gitObject.FetchBranch(releaseBranch1)
			//gitObject.FetchBranch(releaseBranch2)
			// 每一个文件的变更详细内容
			for _, fileName := range typeRepoInfo.FileList {
				commitList, err := gitObject.CommitInfomationByBranch(releaseBranch1, releaseBranch2, fileName)
				if err != nil {
					log.Printf("get commit info in the file[%s] failed: [%s] \n", fileName, err.Error())
					continue
				}
				CMFileOperInfo.FileOper[fileName] = commitList
				//CMResultInfo[configType] = append(CMResultInfo[configType])

			}
			CMFileOperInfoList = append(CMFileOperInfoList, CMFileOperInfo)
		}
		CMResultInfo.CMFileOperInfo[configType] = CMFileOperInfoList
	}
	return CMResultInfo.CMFileOperInfo
}
