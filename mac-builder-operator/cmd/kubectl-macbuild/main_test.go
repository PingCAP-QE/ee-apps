package main

import "testing"

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
