package main

import (
	"context"
	"os"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/controller"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
	"github.com/rs/zerolog/log"
)

func main() {
	prodName := "tidb"
	baseVersion := "v5.4.1"
	prod := service.StringToProduct(prodName)
	if prod == "" {
		log.Error().Str("name", prodName).Msg("bad product name")
		os.Exit(1)
	}

	req := service.BranchCreateReq{Prod: prod, BaseVersion: baseVersion}
	controller.NewChatPrintRepoService().CreateBranch(context.TODO(), req)
}
