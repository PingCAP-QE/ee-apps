package handler

import (
	"context"
	"fmt"

	"github.com/go-resty/resty/v2"
)

const apiURLHotfixCreateBranch = "https://tibuild.pingcap.net/api/hotfix/create-branch"

func runCommandHotfixCreateBranch(_ context.Context, args []string) (string, error) {
	if len(args) < 2 {
		return "", fmt.Errorf("missing required positional arguments: prod version")
	}
	prod := args[0]
	baseVersion := args[1]

	client := resty.New()
	resp, err := client.R().
		SetBody(map[string]any{"prod": prod, "baseVersion": baseVersion}).
		Post(apiURLHotfixCreateBranch)
	if err != nil {
		return "", err
	}
	if !resp.IsSuccess() {
		return "", fmt.Errorf("request failed: %s\n%s", resp.Status(), resp.Body())
	}
	return string(resp.Body()), nil
}
