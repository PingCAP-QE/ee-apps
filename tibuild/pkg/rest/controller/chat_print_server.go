package controller

import (
	"tibuild/pkg/rest/repo"
	"tibuild/pkg/rest/service"
)

func NewChatPrintRepoService() service.HotfixService {
	return service.NewRepoService(repo.ChatPrinter{})
}
