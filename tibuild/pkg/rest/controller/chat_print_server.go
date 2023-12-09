package controller

import (
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/repo"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

func NewChatPrintRepoService() service.HotfixService {
	return service.NewRepoService(repo.ChatPrinter{})
}
