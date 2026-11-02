package gcs

import (
	"context"
	"io"
	"log"
	"testing"

	gcsapi "github.com/PingCAP-QE/ee-apps/dl/gen/gcs"
)

func TestNewRejectsDisallowedBucketBeforeClient(t *testing.T) {
	svc := New(log.New(io.Discard, "", 0), "", []string{"hotfix-binaries"}, nil)

	_, _, err := svc.DownloadObject(context.Background(), &gcsapi.DownloadObjectPayload{
		Bucket: "prow-tidb-logs",
		Key:    "x",
	})
	if err == nil {
		t.Fatal("DownloadObject() error = nil, want rejection")
	}
	if got := err.Error(); got == "" || contains(got, "@") {
		t.Fatalf("DownloadObject() leaked an identity or empty error: %q", got)
	}
}
