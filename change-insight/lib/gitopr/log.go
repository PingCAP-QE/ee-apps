package gitopr

import (
	"errors"
	"log"
	"strings"

	"github.com/PingCAP-QE/ee-apps/change-insight/lib/command"
)

type GitObject struct {
	WS     string // workspace
	Org    string // eg: pingcap
	Repo   string // eg: tidb
	Branch string // eg: master
}

type Commit struct {
	ID       string
	Date     string
	Message  string
	Commiter string
	Mail     string
}

// nolint: unused
func (Git *GitObject) initWorkSpace() error {
	dir := Git.WS
	updateWS := "git pull"
	cloneWS := "git clone git@github.com:" + Git.Org + "/" + Git.Repo
	execCmd := "cd " + dir + " && if [ -d \"" + Git.Repo + "\" ];then cd " +
		Git.Repo + " && " + updateWS + "; else " + cloneWS + "; fi;"
	_, err := command.Cmd(execCmd)
	return err
}

func (Git *GitObject) formateInfo(logString string, sep string) ([]Commit, error) {
	commitInfoList := []Commit{}
	logStringArray := strings.Split(logString, "\n")
	for _, logStringLine := range logStringArray {
		commitInfo := Commit{}
		columArray := strings.Split(logStringLine, sep)
		if len(columArray) != 5 {
			log.Printf("git log result: %+v \nsep [%s]", logStringLine, sep)
			err := errors.New("splite the command result error")
			return commitInfoList, err
		}
		commitInfo.ID = columArray[0]
		commitInfo.Date = columArray[1]
		commitInfo.Message = columArray[2]
		commitInfo.Commiter = columArray[3]
		commitInfo.Mail = columArray[4]

		commitInfoList = append(commitInfoList, commitInfo)
	}
	//log.Printf("commitInfoList: %+v\n", commitInfoList)
	return commitInfoList, nil

}

// CommitInfomationByDate 按照指定时间段来分析commit信息
func (Git *GitObject) CommitInfomationByDate(since string, util string, fileName string) ([]Commit, error) {
	/**
	commitInfoList := []Commit{}
	 err := Git.initWorkSpace()
	if err != nil {
		log.Println("initWorkSpace error:", err)
		return commitInfoList, err
	}
	**/
	sep := "####"
	execCmd := "cd " + Git.WS + "/" + Git.Repo + ";git log  --pretty=format:\"%h" + sep + "%cd" + sep + "%s" +
		sep + "%an" + sep + "%ae\" --since=\"" + since + "\" --until=\"" + util + "\" " + fileName
	logString, err := command.Cmd(execCmd)
	if err != nil {
		log.Printf("exec command failed! comand:[%v], err:[%v]\n", execCmd, err.Error())
	}
	return Git.formateInfo(logString, sep)
}

func (Git *GitObject) FetchBranch(branch string) error {
	execCmd := "cd " + Git.WS + "/" + Git.Repo + ";git fetch origin " + branch
	_, err := command.Cmd(execCmd)
	if err != nil {
		log.Printf("exec command failed! comand:[%v], err:[%v]\n", execCmd, err.Error())
	}
	return err
}

// CommitInfomationByBranch 按照发版之间的diff来分析commit信息
func (Git *GitObject) CommitInfomationByBranch(featureBranchA string, featureBranchB string, fileName string) ([]Commit, error) {
	/*
		commitInfoList := []Commit{}
		err := Git.initWorkSpace()
		if err != nil {
			log.Println("initWorkSpace error:", err)
			return commitInfoList, err
		}
		**/
	sep := "####"

	execCmd := "cd " + Git.WS + "/" + Git.Repo + "; git log  --pretty=format:\"%h" + sep + "%cd" + sep + "%s" +
		sep + "%an" + sep + "%ae\"  `git merge-base " + Git.Branch + " origin/" + featureBranchA + "`..`git merge-base origin/" + Git.Branch + " " + featureBranchB + "` " + fileName
	logString, err := command.Cmd(execCmd)
	if err != nil {
		log.Printf("exec command failed! comand:[%v], err:[%v]\n", execCmd, err.Error())
	}
	return Git.formateInfo(logString, sep)
}
