package oci

import (
	"errors"
	"io"
	"log"
	"testing"
)

func TestGetTargetRepoRejectsDisallowedRegistry(t *testing.T) {
	svc := New(log.New(io.Discard, "", 0), nil, []string{"us-docker.pkg.dev"})

	_, err := svc.(*ocisrvc).GetTargetRepo("kubernetes.default.svc/probe/repo")
	if !errors.Is(err, ErrTargetNotAllowed) {
		t.Fatalf("GetTargetRepo() error = %v, want ErrTargetNotAllowed", err)
	}
	if got := err.Error(); got == "" || contains(got, "kubernetes.default.svc") {
		t.Fatalf("GetTargetRepo() leaked the rejected host: %q", got)
	}
}

func TestGetTargetRepoAllowsConfiguredRegistry(t *testing.T) {
	svc := New(log.New(io.Discard, "", 0), nil, []string{"us-docker.pkg.dev"})

	repo, err := svc.(*ocisrvc).GetTargetRepo("us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/offline-package")
	if err != nil {
		t.Fatalf("GetTargetRepo() unexpected error: %v", err)
	}
	if repo == nil || repo.Reference.Registry != "us-docker.pkg.dev" {
		t.Fatalf("GetTargetRepo() = %#v, want registry us-docker.pkg.dev", repo)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
