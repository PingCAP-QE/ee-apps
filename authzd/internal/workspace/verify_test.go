package workspace

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newUserServer(t *testing.T, id, status string) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-token" {
			t.Errorf("Authorization = %q", r.Header.Get("Authorization"))
		}
		if !strings.HasPrefix(r.URL.Path, "/admin/directory/v1/users/") {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		if status != "" {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		_, _ = w.Write([]byte(`{"id":"` + id + `","primaryEmail":"a@example.com"}`))
	}))
	t.Cleanup(server.Close)
	return server
}

func TestVerifySubjectAcceptsMatchingDirectoryID(t *testing.T) {
	server := newUserServer(t, "102401699953594095413", "")
	verifier, err := NewVerifier(VerifierConfig{BaseURL: server.URL, Token: tokenSource("test-token")})
	if err != nil {
		t.Fatalf("NewVerifier() error = %v", err)
	}
	// Both spellings of the expected value are accepted.
	for _, expected := range []string{
		"102401699953594095413",
		"user:google/sub/102401699953594095413",
	} {
		if err := verifier.VerifySubject(context.Background(), "a@example.com", expected); err != nil {
			t.Fatalf("VerifySubject(%q) error = %v", expected, err)
		}
	}
}

func TestVerifySubjectRejectsMismatch(t *testing.T) {
	server := newUserServer(t, "102401699953594095413", "")
	verifier, err := NewVerifier(VerifierConfig{BaseURL: server.URL, Token: tokenSource("test-token")})
	if err != nil {
		t.Fatal(err)
	}
	err = verifier.VerifySubject(context.Background(), "a@example.com", "user:google/sub/113364093292557124527")
	if err == nil || !strings.Contains(err.Error(), "gate 1 failed") {
		t.Fatalf("VerifySubject() error = %v, want a gate 1 failure", err)
	}
}

func TestVerifySubjectFailsClosed(t *testing.T) {
	failing := newUserServer(t, "", "boom")
	ok := newUserServer(t, "102401699953594095413", "")
	cases := map[string]struct {
		email string
		sub   string
		base  string
		want  string
	}{
		"directory error":     {email: "a@example.com", sub: "1", base: failing.URL, want: "status 500"},
		"missing email":       {email: "", sub: "1", base: ok.URL, want: "email is required"},
		"missing expectation": {email: "a@example.com", sub: "", base: ok.URL, want: "expected subject is required"},
	}
	for name, testCase := range cases {
		t.Run(name, func(t *testing.T) {
			verifier, err := NewVerifier(VerifierConfig{BaseURL: testCase.base, Token: tokenSource("test-token")})
			if err != nil {
				t.Fatal(err)
			}
			if err := verifier.VerifySubject(context.Background(), testCase.email, testCase.sub); err == nil ||
				!strings.Contains(err.Error(), testCase.want) {
				t.Fatalf("VerifySubject() error = %v, want one containing %q", err, testCase.want)
			}
		})
	}
}

func TestVerifierRejectsNonNumericDirectoryID(t *testing.T) {
	server := newUserServer(t, "not-a-number", "")
	verifier, err := NewVerifier(VerifierConfig{BaseURL: server.URL, Token: tokenSource("test-token")})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := verifier.DirectoryID(context.Background(), "a@example.com"); err == nil ||
		!strings.Contains(err.Error(), "non-numeric") {
		t.Fatalf("DirectoryID() error = %v", err)
	}
}

func TestNewVerifierRequiresCredential(t *testing.T) {
	if _, err := NewVerifier(VerifierConfig{}); err == nil {
		t.Fatal("NewVerifier() accepted a nil credential source")
	}
	if _, err := NewVerifier(VerifierConfig{Token: tokenSource("t"), BaseURL: "file:///tmp"}); err == nil {
		t.Fatal("NewVerifier() accepted a non-HTTP base URL")
	}
}
