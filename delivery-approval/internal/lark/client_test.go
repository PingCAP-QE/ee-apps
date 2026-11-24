package lark

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	larksuite "github.com/larksuite/oapi-sdk-go/v3"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/config"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/delivery"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

type larkServer struct {
	mu       sync.Mutex
	messages []sentMessage
	urgent   []string
	batches  int
	failCode int
}

type sentMessage struct {
	receiveIDType string
	receiveID     string
	content       string
}

func (s *larkServer) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.Contains(r.URL.Path, "/auth/v3/"):
			_, _ = io.WriteString(w, `{"code":0,"tenant_access_token":"tenant-token","app_access_token":"app-token","expire":7200}`)
		case strings.HasSuffix(r.URL.Path, "/contact/v3/users/batch_get_id"):
			var body struct {
				Emails []string `json:"emails"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			s.mu.Lock()
			s.batches++
			s.mu.Unlock()
			users := make([]map[string]string, 0, len(body.Emails))
			for _, email := range body.Emails {
				switch email {
				case "approver@example.com":
					users = append(users, map[string]string{"user_id": "ou-approver", "email": email})
				case "requester@example.com":
					users = append(users, map[string]string{"user_id": "ou-requester", "email": email})
				}
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": map[string]any{"user_list": users}})
		case strings.Contains(r.URL.Path, "/urgent_app"):
			s.mu.Lock()
			s.urgent = append(s.urgent, r.URL.Path)
			s.mu.Unlock()
			_, _ = io.WriteString(w, `{"code":0,"data":{}}`)
		case strings.HasSuffix(r.URL.Path, "/im/v1/messages"):
			var body struct {
				ReceiveID string `json:"receive_id"`
				Content   string `json:"content"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			s.mu.Lock()
			s.messages = append(s.messages, sentMessage{receiveIDType: r.URL.Query().Get("receive_id_type"), receiveID: body.ReceiveID, content: body.Content})
			s.mu.Unlock()
			if s.failCode != 0 {
				_, _ = io.WriteString(w, `{"code":999,"msg":"failed"}`)
				return
			}
			_, _ = io.WriteString(w, `{"code":0,"data":{"message_id":"om-1"}}`)
		default:
			http.NotFound(w, r)
		}
	}
}

func testConfig(t *testing.T) *config.Manager {
	t.Helper()
	cfg := config.Default()
	cfg.PublicURL = "https://approvals.example.com"
	cfg.DLBaseURL = "http://dl-management:80"
	cfg.OIDCClientID = "audience"
	cfg.AllowedEmailDomains = []string{"example.com"}
	cfg.ApproverEmails = []string{"approver@example.com"}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	return config.NewManager(cfg)
}

func testClient(t *testing.T, s *larkServer) *Client {
	t.Helper()
	server := httptest.NewServer(s.handler())
	t.Cleanup(server.Close)
	api := larksuite.NewClient("app-id", "app-secret", larksuite.WithOpenBaseUrl(server.URL))
	c, err := New(api, "oc-chat", testConfig(t), true, nil)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func sampleRequest() *store.Request {
	return &store.Request{
		ID: "req-1", RequesterEmail: "requester@example.com", TTLSeconds: 3600,
		ExpiresAt: time.Now().UTC().Add(time.Hour),
		Files: []store.File{
			{Repository: "r.example.com/a", Tag: "v1", Filename: "a.tar.gz"},
			{Repository: "r.example.com/a", Tag: "v1", Filename: "b.tar.gz"},
		},
	}
}

func TestNotifyApprovalSendsCardAndBuzz(t *testing.T) {
	s := &larkServer{}
	c := testClient(t, s)
	if err := c.NotifyApproval(context.Background(), sampleRequest()); err != nil {
		t.Fatal(err)
	}
	if len(s.messages) != 1 || s.messages[0].receiveIDType != "chat_id" || s.messages[0].receiveID != "oc-chat" {
		t.Fatalf("messages = %+v", s.messages)
	}
	if strings.Contains(s.messages[0].content, "dl.pingcap.net") {
		t.Fatalf("approval card contains a download URL: %s", s.messages[0].content)
	}
	if !strings.Contains(s.messages[0].content, "approvals.example.com") {
		t.Fatalf("approval card missing review link: %s", s.messages[0].content)
	}
	if len(s.urgent) != 1 || !strings.Contains(s.urgent[0], "om-1") {
		t.Fatalf("urgent = %v", s.urgent)
	}
}

func TestNotifySendsLinksAndCachesOpenID(t *testing.T) {
	s := &larkServer{}
	c := testClient(t, s)
	links := []delivery.Link{
		{File: store.File{Filename: "a.tar.gz", Repository: "r.example.com/a", Tag: "v1"}, URL: "https://dl.example.com/d/one"},
		{File: store.File{Filename: "b.tar.gz", Repository: "r.example.com/a", Tag: "v1"}, URL: "https://dl.example.com/d/two"},
	}
	if err := c.Notify(context.Background(), sampleRequest(), links); err != nil {
		t.Fatal(err)
	}
	if err := c.Notify(context.Background(), sampleRequest(), links); err != nil {
		t.Fatal(err)
	}
	if len(s.messages) != 2 || !strings.Contains(s.messages[0].content, "https://dl.example.com/d/one") {
		t.Fatalf("messages = %+v", s.messages)
	}
	if s.batches != 1 {
		t.Fatalf("batch lookups = %d, want 1 (cache)", s.batches)
	}
}

func TestNotifyUnknownUser(t *testing.T) {
	s := &larkServer{}
	c := testClient(t, s)
	req := sampleRequest()
	req.RequesterEmail = "unknown@example.com"
	if err := c.Notify(context.Background(), req, []delivery.Link{{URL: "https://dl.example.com/d/x"}}); err == nil {
		t.Fatal("unknown user accepted")
	}
}

func TestNotifyError(t *testing.T) {
	s := &larkServer{failCode: 999}
	c := testClient(t, s)
	if err := c.Notify(context.Background(), sampleRequest(), []delivery.Link{{URL: "https://dl.example.com/d/x"}}); err == nil {
		t.Fatal("lark error accepted")
	}
}

func TestNotifyApprovalDMsEachApprover(t *testing.T) {
	s := &larkServer{}
	c := testClient(t, s)
	c.chatID = ""
	if err := c.NotifyApproval(context.Background(), sampleRequest()); err != nil {
		t.Fatal(err)
	}
	if len(s.messages) != 1 || s.messages[0].receiveIDType != "open_id" || s.messages[0].receiveID != "ou-approver" {
		t.Fatalf("messages = %+v", s.messages)
	}
	if !strings.Contains(s.messages[0].content, "approvals.example.com") {
		t.Fatalf("DM card missing review link: %s", s.messages[0].content)
	}
	if len(s.urgent) != 0 {
		t.Fatalf("DM mode should not call urgent: %v", s.urgent)
	}
}

func TestNewValidation(t *testing.T) {
	if _, err := New(nil, "chat", testConfig(t), false, nil); err == nil {
		t.Fatal("nil api accepted")
	}
	if _, err := NewDefault("", "", "chat", testConfig(t), false, nil); err == nil {
		t.Fatal("missing credentials accepted")
	}
	if _, err := NewDefault("id", "secret", "", testConfig(t), false, nil); err != nil {
		t.Fatalf("empty chat should enable per-approver DM: %v", err)
	}
}

func TestNotifyDecisionAndFailure(t *testing.T) {
	s := &larkServer{}
	c := testClient(t, s)
	if err := c.NotifyDecision(context.Background(), sampleRequest(), "rejected", "not eligible"); err != nil {
		t.Fatal(err)
	}
	if err := c.NotifyFailure(context.Background(), sampleRequest(), "notify_failed"); err != nil {
		t.Fatal(err)
	}
	if len(s.messages) != 2 {
		t.Fatalf("messages = %+v", s.messages)
	}
	if !strings.Contains(s.messages[0].content, "rejected") || !strings.Contains(s.messages[0].content, "not eligible") {
		t.Fatalf("decision message = %s", s.messages[0].content)
	}
	if !strings.Contains(s.messages[1].content, "delivery failed") && !strings.Contains(s.messages[1].content, "Delivery for request") {
		t.Fatalf("failure message = %s", s.messages[1].content)
	}
}
