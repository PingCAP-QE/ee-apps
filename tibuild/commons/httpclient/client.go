package httpclient

import (
	"time"

	"io/ioutil"
	"net/http"
)

var (
	// 3 seconds timeout
	httpClient = &http.Client{Timeout: time.Second * 3}
)

// Get request by url
func Get(url string) (string, error) {
	resp, err := httpClient.Get(url)
	if nil != err || http.StatusOK != resp.StatusCode {
		return "", err
	}
	data, err := ioutil.ReadAll(resp.Body)
	return string(data), err
}
