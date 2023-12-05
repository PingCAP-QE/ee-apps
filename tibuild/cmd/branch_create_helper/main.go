package main

import (
	"context"
	"fmt"
	"tibuild/pkg/rest/controller"
	"tibuild/pkg/rest/service"
)

func main() {
	prodName := "tidb"
	baseVersion := "v5.4.1"
	prod := service.StringToProduct(prodName)
	if prod == service.ProductUnknown {
		fmt.Println("bad prod name" + prodName)
	}
	controller.NewChatPrintRepoService().CreateBranch(context.TODO(), service.BranchCreateReq{Prod: prod, BaseVersion: baseVersion})
}
