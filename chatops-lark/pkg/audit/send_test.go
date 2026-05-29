package audit

import (
	"strings"
	"testing"
)

func TestNewLarkCardWithGoTemplate(t *testing.T) {
	t.Run("success with original template", func(t *testing.T) {
		result := "TiDB-X hotfix tag bumped successfully:\n• Repo:   PingCAP-QE/ci\n• Commit: e2cc653b672029b78519a524847b341baf72c98f\n• Tag:    v8.5.4-nextgen.202510.2"
		info := &AuditInfo{
			UserEmail: "user@example.com",
			Command:   "/devbuild",
			Args:      []string{"--foo", "bar"},
			Result:    &result,
		}
		str, err := newLarkCardWithGoTemplate(info)
		t.Log(str)
		if err != nil {
			t.Fatalf("expected no error, got: %v", err)
		}
	})

	t.Run("preserves quotes in result markdown", func(t *testing.T) {
		result := "version: \"9\""
		info := &AuditInfo{
			UserEmail: "user@example.com",
			Command:   "/cloud-image",
			Result:    &result,
		}
		str, err := newLarkCardWithGoTemplate(info)
		if err != nil {
			t.Fatalf("expected no error, got: %v", err)
		}
		if strings.Contains(str, "&#34;") {
			t.Fatalf("expected rendered audit card to preserve quotes, got: %s", str)
		}
	})
}
