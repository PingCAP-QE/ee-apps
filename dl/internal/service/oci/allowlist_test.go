package oci

import (
	"errors"
	"testing"
)

func TestValidateTargetRepository(t *testing.T) {
	allowed := []string{
		"us-docker.pkg.dev",
		"hub.pingcap.net",
		"hub-zot.pingcap.net",
	}
	tests := []struct {
		name    string
		repo    string
		wantErr bool
	}{
		{name: "allowed registry", repo: "us-docker.pkg.dev/pingcap-testing-account/hub/pingcap/offline-package"},
		{name: "allowed registry uppercase host", repo: "US-DOCKER.PKG.DEV/pingcap-testing-account/hub/pingcap/offline-package"},
		{name: "allowed hub", repo: "hub.pingcap.net/pingcap/tidb/package"},
		{name: "allowed hub with port", repo: "hub.pingcap.net:443/pingcap/tidb/package"},
		{name: "cluster service", repo: "kubernetes.default.svc/probe/repo", wantErr: true},
		{name: "loopback ipv4", repo: "127.0.0.1:5000/repo", wantErr: true},
		{name: "metadata ipv4", repo: "169.254.169.254/latest/meta-data", wantErr: true},
		{name: "private ipv4", repo: "10.0.0.1/repo", wantErr: true},
		{name: "loopback ipv6", repo: "[::1]/repo", wantErr: true},
		{name: "unlisted public host", repo: "evil.example.com/repo", wantErr: true},
		{name: "scheme", repo: "https://us-docker.pkg.dev/repo", wantErr: true},
		{name: "userinfo", repo: "user@us-docker.pkg.dev/repo", wantErr: true},
		{name: "missing path", repo: "us-docker.pkg.dev", wantErr: true},
		{name: "empty", repo: "", wantErr: true},
		{name: "localhost", repo: "localhost/repo", wantErr: true},
		{name: "svc suffix", repo: "anything.svc/repo", wantErr: true},
		{name: "local suffix", repo: "anything.local/repo", wantErr: true},
		{name: "metadata host", repo: "metadata.google.internal/repo", wantErr: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := ValidateTargetRepository(tt.repo, allowed)
			if tt.wantErr && !errors.Is(err, ErrTargetNotAllowed) && !errors.Is(err, ErrInvalidRepository) {
				t.Fatalf("ValidateTargetRepository(%q) error = %v, want target-not-allowed or invalid-repository", tt.repo, err)
			}
			if !tt.wantErr && err != nil {
				t.Fatalf("ValidateTargetRepository(%q) unexpected error: %v", tt.repo, err)
			}
		})
	}
}

func TestIsTargetErrorAndInvalidFileError(t *testing.T) {
	if !IsTargetError(ErrTargetNotAllowed) || !IsTargetError(ErrInvalidRepository) {
		t.Fatal("IsTargetError did not recognize target errors")
	}
	if IsTargetError(errors.New("other")) {
		t.Fatal("IsTargetError recognized an unrelated error")
	}

	denied := invalidFileError(ErrTargetNotAllowed)
	if denied == nil || denied.Error() == "" {
		t.Fatal("invalidFileError returned an empty error")
	}
	other := invalidFileError(errors.New("upstream TLS failure"))
	if other == nil || other.Error() == "" {
		t.Fatal("invalidFileError returned an empty error")
	}
}
