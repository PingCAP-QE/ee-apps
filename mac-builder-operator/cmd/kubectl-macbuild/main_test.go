package main

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestBuildRequestServerOverride(t *testing.T) {
	t.Parallel()

	client, endpoint, err := buildRequest(options{
		server:    "http://127.0.0.1:8080/",
		namespace: "ee-cd",
	}, "macbuild-abc")
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	if client == nil {
		t.Fatal("expected an HTTP client")
	}
	want := "http://127.0.0.1:8080/v1/logs/ee-cd/macbuild-abc"
	if endpoint != want {
		t.Fatalf("unexpected endpoint:\nwant %s\n got %s", want, endpoint)
	}
}

func TestSplitArgsAllowsNameBeforeOrAfterFlags(t *testing.T) {
	t.Parallel()

	name, flags, err := splitArgs([]string{"build", "-n", "ee-cd", "--tail", "5"})
	if err != nil {
		t.Fatalf("split name-first: %v", err)
	}
	if name != "build" || strings.Join(flags, " ") != "-n ee-cd --tail 5" {
		t.Fatalf("unexpected name-first split: %q %v", name, flags)
	}

	name, flags, err = splitArgs([]string{"-n", "ee-cd", "--tail=5", "build"})
	if err != nil {
		t.Fatalf("split flags-first: %v", err)
	}
	if name != "build" || strings.Join(flags, " ") != "-n ee-cd --tail=5" {
		t.Fatalf("unexpected flags-first split: %q %v", name, flags)
	}
}

func TestRunRejectsBadUsage(t *testing.T) {
	t.Parallel()

	cases := [][]string{
		nil,
		{"get"},
		{"logs"},
		{"logs", "one", "two"},
		{"logs", "--bad-flag", "build"},
	}
	for _, args := range cases {
		var stdout, stderr bytes.Buffer
		if code := run(args, &stdout, &stderr); code != 2 {
			t.Fatalf("args %v: expected exit 2, got %d", args, code)
		}
		if stderr.Len() == 0 {
			t.Fatalf("args %v: expected usage or flag error on stderr", args)
		}
	}
}

func TestRunFetchesLog(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/logs/ee-cd/build" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if r.URL.Query().Get("tail") != "5" {
			t.Errorf("unexpected tail: %q", r.URL.Query().Get("tail"))
		}
		if r.URL.Query().Get("follow") != "true" {
			t.Errorf("expected follow=true")
		}
		_, _ = io.WriteString(w, "hello\n")
	}))
	defer server.Close()

	var stdout, stderr bytes.Buffer
	args := []string{"logs", "build", "-n", "ee-cd", "--tail", "5", "--follow", "--server", server.URL}
	if code := run(args, &stdout, &stderr); code != 0 {
		t.Fatalf("expected exit 0, got %d: %s", code, stderr.String())
	}
	if stdout.String() != "hello\n" {
		t.Fatalf("unexpected stdout: %q", stdout.String())
	}
}

func TestRunReportsServerErrors(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "log not found", http.StatusNotFound)
	}))
	defer server.Close()

	var stdout, stderr bytes.Buffer
	if code := run([]string{"logs", "build", "--server", server.URL}, &stdout, &stderr); code != 1 {
		t.Fatalf("expected exit 1, got %d", code)
	}
	if !strings.Contains(stderr.String(), "404") {
		t.Fatalf("expected the status in stderr: %s", stderr.String())
	}
}

func TestRunPrintsProxyHintOnForbidden(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "forbidden", http.StatusForbidden)
	}))
	defer server.Close()

	var stdout, stderr bytes.Buffer
	args := []string{"logs", "build", "--server", server.URL, "--log-service", "logs"}
	if code := run(args, &stdout, &stderr); code != 1 {
		t.Fatalf("expected exit 1, got %d", code)
	}
	if !strings.Contains(stderr.String(), "port-forward") {
		t.Fatalf("expected the port-forward hint: %s", stderr.String())
	}
}
