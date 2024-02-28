package lark

import (
	"crypto/tls"
	"net/http"

	larksdk "github.com/larksuite/oapi-sdk-go/v3"
)

func NewClient(appID, appSecret string) *larksdk.Client {
	// Disable certificate verification
	tr := &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
	}
	httpClient := &http.Client{Transport: tr}

	return larksdk.NewClient(appID, appSecret,
		larksdk.WithLogReqAtDebug(true),
		larksdk.WithEnableTokenCache(true),
		larksdk.WithHttpClient(httpClient),
	)
}
