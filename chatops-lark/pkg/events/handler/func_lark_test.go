package handler

import (
	"testing"

	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
)

func TestNormalizeCommandToken(t *testing.T) {
	tests := []struct {
		name  string
		token string
		want  string
	}{
		{
			name:  "plain token",
			token: "ghcr.io/pingcap/tidb",
			want:  "ghcr.io/pingcap/tidb",
		},
		{
			name:  "markdown link token",
			token: "[tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm](http://tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm)",
			want:  "tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm",
		},
		{
			name:  "invalid markdown link token",
			token: "[broken-link]",
			want:  "[broken-link]",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := normalizeCommandToken(tt.token); got != tt.want {
				t.Fatalf("normalizeCommandToken() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestParsePrivateCommandNormalizesMarkdownLinkArgs(t *testing.T) {
	content := `{"text":"/cloud-image query --repo [tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm](http://tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm) --tag v26.3.0-nextgen"}`

	command := parsePrivateCommand(&larkim.P2MessageReceiveV1{
		Event: &larkim.P2MessageReceiveV1Data{
			Message: &larkim.EventMessage{
				Content:  &content,
				ChatType: stringPtr("p2p"),
			},
		},
	})
	if command == nil {
		t.Fatal("expected private command to be parsed")
	}

	wantArgs := []string{"query", "--repo", "tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm", "--tag", "v26.3.0-nextgen"}
	if command.Name != "/cloud-image" {
		t.Fatalf("parsePrivateCommand() name = %q, want %q", command.Name, "/cloud-image")
	}
	if len(command.Args) != len(wantArgs) {
		t.Fatalf("parsePrivateCommand() args len = %d, want %d (%v)", len(command.Args), len(wantArgs), command.Args)
	}
	for i := range wantArgs {
		if command.Args[i] != wantArgs[i] {
			t.Fatalf("parsePrivateCommand() arg[%d] = %q, want %q; args=%v", i, command.Args[i], wantArgs[i], command.Args)
		}
	}
}

func TestParseGroupCommandNormalizesMarkdownLinkArgs(t *testing.T) {
	content := `{"text":"@_user_1 /cloud-image query --repo [tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm](http://tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm) --tag v26.3.0-nextgen"}`

	command := parseGroupCommand(&larkim.P2MessageReceiveV1{
		Event: &larkim.P2MessageReceiveV1Data{
			Message: &larkim.EventMessage{
				Content:  &content,
				ChatType: stringPtr("group"),
			},
		},
	})
	if command == nil {
		t.Fatal("expected group command to be parsed")
	}

	wantArgs := []string{"query", "--repo", "tidbcloud-prod-registry.ap-southeast-1.cr.aliyuncs.com/tidbcloud/dm", "--tag", "v26.3.0-nextgen"}
	if command.Name != "/cloud-image" {
		t.Fatalf("parseGroupCommand() name = %q, want %q", command.Name, "/cloud-image")
	}
	if len(command.Args) != len(wantArgs) {
		t.Fatalf("parseGroupCommand() args len = %d, want %d (%v)", len(command.Args), len(wantArgs), command.Args)
	}
	for i := range wantArgs {
		if command.Args[i] != wantArgs[i] {
			t.Fatalf("parseGroupCommand() arg[%d] = %q, want %q; args=%v", i, command.Args[i], wantArgs[i], command.Args)
		}
	}
}

func stringPtr(v string) *string {
	return &v
}
