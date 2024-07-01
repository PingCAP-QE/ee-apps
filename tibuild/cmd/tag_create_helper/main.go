package main

import (
	"context"
	"os"

	"github.com/rs/zerolog/log"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/controller"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/rest/service"
)

func main() {
	prodName := "br"
	branch := "release-5.4-20220903-v5.4.1"
	prod := service.StringToProduct(prodName)
	if prod == "" {
		log.Error().Str("name", prodName).Msg("bad product name")
		os.Exit(1)
	}

	req := service.TagCreateReq{Prod: prod, Branch: branch}
	controller.NewChatPrintRepoService().CreateTag(context.TODO(), req)
}
