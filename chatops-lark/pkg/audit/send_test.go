package audit

import (
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
}
