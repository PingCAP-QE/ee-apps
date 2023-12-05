package main

import (
	"context"
	"fmt"
	"tibuild/pkg/rest/controller"
	"tibuild/pkg/rest/service"
)

func main() {
	prodName := "br"
	branch := "release-5.4-20220903-v5.4.1"
	prod := service.StringToProduct(prodName)
	if prod == service.ProductUnknown {
		fmt.Println("bad prod name: " + prodName)
	}
	controller.NewChatPrintRepoService().CreateTag(context.TODO(), service.TagCreateReq{Prod: prod, Branch: branch})
}
