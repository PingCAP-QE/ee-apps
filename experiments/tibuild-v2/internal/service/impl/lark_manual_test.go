//go:build manual

package impl

import (
	"context"
	"os"
	"testing"

	larksdk "github.com/larksuite/oapi-sdk-go/v3"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
)

func TestSendLarkCard_Manual(t *testing.T) {
	appID := os.Getenv("LARK_APP_ID")
	appSecret := os.Getenv("LARK_APP_SECRET")
	if appID == "" || appSecret == "" {
		t.Skip("set LARK_APP_ID and LARK_APP_SECRET to run this test")
	}

	receiver := os.Getenv("LARK_RECEIVER")
	if receiver == "" {
		t.Skip("set LARK_RECEIVER to your Lark email or open_id to run this test")
	}

	info := &NotificationInfo{
		BuildID:   9999,
		Product:   "pd",
		Version:   "v9.0.0-test-card",
		Status:    "PROCESSING",
		CreatedBy: receiver,
		CreatedAt: "2026-07-02T10:00:00Z",
		GitRef:    "branch/master",
		GithubRepo: "tikv/pd",
		Platform:   "linux/amd64",
		GitSha:     "5c811b2194d80b3652898d58e6877b531e7d7f67",
		Images: []ImageInfo{
			{Platform: "linux/amd64", URL: "example.com/devbuild/pd:v9.0.0-test-card"},
			{Platform: "linux/arm64", URL: "example.com/devbuild/pd:v9.0.0-test-card-arm"},
		},
		Binaries: []BinaryInfo{
			{OciReference: "example.com/devbuild/pd:v9.0.0-test-card/pd-server-linux-amd64.tar.gz"},
		},
		PipelineRuns: []PipelineRunInfo{
			{Name: "build-linux-amd64", Status: "Succeeded", URL: "https://tekton.example.com/run/1"},
			{Name: "build-linux-arm64", Status: "Running", URL: "https://tekton.example.com/run/2"},
		},
	}

	cardStr, err := NewLarkCardJSON(info)
	if err != nil {
		t.Fatalf("build card: %v", err)
	}
	t.Logf("Card JSON:\n%s\n", cardStr)

	ctx := context.Background()
	client := larksdk.NewClient(appID, appSecret, larksdk.WithEnableTokenCache(true))

	req := larkim.NewCreateMessageReqBuilder().
		ReceiveIdType("email").
		Body(larkim.NewCreateMessageReqBodyBuilder().
			MsgType(larkim.MsgTypeInteractive).
			ReceiveId(receiver).
			Content(cardStr).
			Build()).
		Build()

	resp, err := client.Im.Message.Create(ctx, req)
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	if !resp.Success() {
		t.Fatalf("API error: code=%d msg=%s", resp.Code, resp.Msg)
	}
	t.Logf("Sent! msg_id=%s", *resp.Data.MessageId)
}

func TestSendLarkCard_Success_Manual(t *testing.T) {
	appID := os.Getenv("LARK_APP_ID")
	appSecret := os.Getenv("LARK_APP_SECRET")
	if appID == "" || appSecret == "" {
		t.Skip("set LARK_APP_ID and LARK_APP_SECRET to run this test")
	}

	receiver := os.Getenv("LARK_RECEIVER")
	if receiver == "" {
		t.Skip("set LARK_RECEIVER to your Lark email or open_id to run this test")
	}

	info := &NotificationInfo{
		BuildID:   9998,
		Product:   "tikv",
		Version:   "v8.5.0",
		Status:    "SUCCESS",
		CreatedBy: receiver,
		CreatedAt: "2026-07-02T09:00:00Z",
		CompletedAt: "2026-07-02T09:30:00Z",
		GitRef:    "tag/v8.5.0",
		GithubRepo: "tikv/tikv",
		Platform:   "linux/arm64",
		GitSha:     "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
		ErrMsg:     "",
		Images: []ImageInfo{
			{Platform: "linux/arm64", URL: "example.com/release/tikv:v8.5.0"},
		},
		Binaries: []BinaryInfo{
			{OciReference: "example.com/release/tikv:v8.5.0/tikv-server-linux-arm64.tar.gz"},
		},
		PipelineRuns: []PipelineRunInfo{
			{Name: "build-tikv-release-arm64", Status: "Succeeded", URL: "https://tekton.example.com/run/100"},
		},
	}

	cardStr, err := NewLarkCardJSON(info)
	if err != nil {
		t.Fatalf("build card: %v", err)
	}
	t.Logf("Card JSON:\n%s\n", cardStr)

	ctx := context.Background()
	client := larksdk.NewClient(appID, appSecret, larksdk.WithEnableTokenCache(true))

	req := larkim.NewCreateMessageReqBuilder().
		ReceiveIdType("email").
		Body(larkim.NewCreateMessageReqBodyBuilder().
			MsgType(larkim.MsgTypeInteractive).
			ReceiveId(receiver).
			Content(cardStr).
			Build()).
		Build()

	resp, err := client.Im.Message.Create(ctx, req)
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	if !resp.Success() {
		t.Fatalf("API error: code=%d msg=%s", resp.Code, resp.Msg)
	}
	t.Logf("Sent! msg_id=%s", *resp.Data.MessageId)
}
