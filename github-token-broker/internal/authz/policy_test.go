package authz

import (
	"errors"
	"testing"
)

func testPolicy(max map[string]string) *Policy {
	return &Policy{Rules: []Rule{{
		Caller: Caller{Namespace: "ci", ServiceAccount: "checkout"},
		Allow: []Allow{{
			Owner:          "example-org",
			Repositories:   []string{"example-repo", "another-repo"},
			MaxPermissions: max,
		}},
	}}}
}

func TestAuthorize(t *testing.T) {
	caller := Caller{Namespace: "ci", ServiceAccount: "checkout"}

	t.Run("default read permission", func(t *testing.T) {
		policy := testPolicy(map[string]string{"contents": "read"})
		perms, err := policy.Authorize(caller, "example-org", "example-repo", nil, nil)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if perms["contents"] != "read" {
			t.Fatalf("expected contents=read, got %#v", perms)
		}
	})

	t.Run("explicit read", func(t *testing.T) {
		policy := testPolicy(map[string]string{"contents": "read"})
		if _, err := policy.Authorize(caller, "example-org", "example-repo", []string{"example-repo"}, map[string]string{"contents": "read"}); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("write over read maximum", func(t *testing.T) {
		policy := testPolicy(map[string]string{"contents": "read"})
		_, err := policy.Authorize(caller, "example-org", "example-repo", nil, map[string]string{"contents": "write"})
		if !errors.Is(err, ErrPermissionNotAllowed) {
			t.Fatalf("expected ErrPermissionNotAllowed, got %v", err)
		}
	})

	t.Run("unknown permission", func(t *testing.T) {
		policy := testPolicy(map[string]string{"contents": "read"})
		_, err := policy.Authorize(caller, "example-org", "example-repo", nil, map[string]string{"pull_requests": "read"})
		if !errors.Is(err, ErrPermissionNotAllowed) {
			t.Fatalf("expected ErrPermissionNotAllowed, got %v", err)
		}
	})

	t.Run("invalid permission level", func(t *testing.T) {
		policy := testPolicy(map[string]string{"contents": "read"})
		_, err := policy.Authorize(caller, "example-org", "example-repo", nil, map[string]string{"contents": "admin"})
		if !errors.Is(err, ErrPermissionNotAllowed) {
			t.Fatalf("expected ErrPermissionNotAllowed, got %v", err)
		}
	})

	t.Run("repository not allowed", func(t *testing.T) {
		policy := testPolicy(map[string]string{"contents": "read"})
		_, err := policy.Authorize(caller, "example-org", "tools-repo", nil, nil)
		if !errors.Is(err, ErrRepositoryNotAllowed) {
			t.Fatalf("expected ErrRepositoryNotAllowed, got %v", err)
		}
	})

	t.Run("narrowing repository not allowed", func(t *testing.T) {
		policy := testPolicy(map[string]string{"contents": "read"})
		_, err := policy.Authorize(caller, "example-org", "example-repo", []string{"example-repo", "tools-repo"}, nil)
		if !errors.Is(err, ErrRepositoryNotAllowed) {
			t.Fatalf("expected ErrRepositoryNotAllowed, got %v", err)
		}
	})

	t.Run("caller mismatch", func(t *testing.T) {
		policy := testPolicy(map[string]string{"contents": "read"})
		_, err := policy.Authorize(Caller{Namespace: "default", ServiceAccount: "checkout"}, "example-org", "example-repo", nil, nil)
		if !errors.Is(err, ErrCallerNotAllowed) {
			t.Fatalf("expected ErrCallerNotAllowed, got %v", err)
		}
	})

	t.Run("owner mismatch", func(t *testing.T) {
		policy := testPolicy(map[string]string{"contents": "read"})
		_, err := policy.Authorize(caller, "pingcap", "tidb", nil, nil)
		if !errors.Is(err, ErrCallerNotAllowed) {
			t.Fatalf("expected ErrCallerNotAllowed, got %v", err)
		}
	})

	t.Run("wildcard repositories", func(t *testing.T) {
		policy := &Policy{Rules: []Rule{{
			Caller: caller,
			Allow: []Allow{{
				Owner:          "example-org",
				Repositories:   []string{"*"},
				MaxPermissions: map[string]string{"contents": "read"},
			}},
		}}}
		if _, err := policy.Authorize(caller, "example-org", "anything", []string{"anything", "another"}, nil); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	t.Run("wildcard owner", func(t *testing.T) {
		policy := &Policy{Rules: []Rule{{
			Caller: caller,
			Allow: []Allow{{
				Owner:          "*",
				Repositories:   []string{"*"},
				MaxPermissions: map[string]string{"members": "read"},
			}},
		}}}
		if _, err := policy.Authorize(caller, "pingcap", "tidb", nil, map[string]string{"members": "read"}); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})
}

func TestPolicyValidate(t *testing.T) {
	tests := []struct {
		name   string
		policy *Policy
	}{
		{name: "nil", policy: nil},
		{name: "no rules", policy: &Policy{}},
		{name: "missing caller", policy: &Policy{Rules: []Rule{{Allow: []Allow{{Owner: "x", MaxPermissions: map[string]string{"contents": "read"}}}}}}},
		{name: "missing allow", policy: &Policy{Rules: []Rule{{Caller: Caller{Namespace: "ns", ServiceAccount: "sa"}}}}},
		{name: "missing owner", policy: &Policy{Rules: []Rule{{Caller: Caller{Namespace: "ns", ServiceAccount: "sa"}, Allow: []Allow{{MaxPermissions: map[string]string{"contents": "read"}}}}}}},
		{name: "missing permissions", policy: &Policy{Rules: []Rule{{Caller: Caller{Namespace: "ns", ServiceAccount: "sa"}, Allow: []Allow{{Owner: "x"}}}}}},
		{name: "bad permission", policy: &Policy{Rules: []Rule{{Caller: Caller{Namespace: "ns", ServiceAccount: "sa"}, Allow: []Allow{{Owner: "x", MaxPermissions: map[string]string{"contents": "admin"}}}}}}},
		{name: "empty repository", policy: &Policy{Rules: []Rule{{Caller: Caller{Namespace: "ns", ServiceAccount: "sa"}, Allow: []Allow{{Owner: "x", Repositories: []string{""}, MaxPermissions: map[string]string{"contents": "read"}}}}}}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if err := tt.policy.Validate(); err == nil {
				t.Fatal("expected an error")
			}
		})
	}

	t.Run("normalizes keys", func(t *testing.T) {
		policy := testPolicy(map[string]string{" Contents ": "READ"})
		if err := policy.Validate(); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if policy.Rules[0].Allow[0].MaxPermissions["contents"] != "read" {
			t.Fatalf("unexpected normalized permissions: %#v", policy.Rules[0].Allow[0].MaxPermissions)
		}
	})
}
