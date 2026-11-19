package lark

import (
	"crypto/tls"
	"net/http"
	"time"

	larksdk "github.com/larksuite/oapi-sdk-go/v3"
)

func NewClient(appID, appSecret string) *larksdk.Client {
	// Certificates are verified: every call carries an app secret, so an
	// intercepted connection would hand over a tenant-wide credential.
	httpClient := &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12},
		},
	}

	return larksdk.NewClient(appID, appSecret,
		larksdk.WithLogReqAtDebug(true),
		larksdk.WithEnableTokenCache(true),
		larksdk.WithHttpClient(httpClient),
	)
}
