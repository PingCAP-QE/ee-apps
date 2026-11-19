package oci

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/opencontainers/go-digest"
	"github.com/opencontainers/image-spec/specs-go"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"oras.land/oras-go/v2/content"
	"oras.land/oras-go/v2/content/memory"
	"oras.land/oras-go/v2/registry/remote"
)

func pushManifest(t *testing.T, store *memory.Store, layers []ocispec.Descriptor) ocispec.Descriptor {
	t.Helper()
	data, err := json.Marshal(ocispec.Manifest{Versioned: specs.Versioned{SchemaVersion: 2}, Layers: layers})
	if err != nil {
		t.Fatal(err)
	}
	desc := content.NewDescriptorFromBytes(ocispec.MediaTypeImageManifest, data)
	if err = store.Push(context.Background(), desc, bytes.NewReader(data)); err != nil {
		t.Fatal(err)
	}
	if err = store.Tag(context.Background(), desc, "v1"); err != nil {
		t.Fatal(err)
	}
	return desc
}

func TestResolveDeliveryFilePinsTagAndRejectsAmbiguity(t *testing.T) {
	store := memory.New()
	layer := content.NewDescriptorFromBytes("application/octet-stream", []byte("original"))
	layer.Annotations = map[string]string{AnnotationKeyFileName: "tidb.tar.gz"}
	first := pushManifest(t, store, []ocispec.Descriptor{layer})
	manifest, blob, err := ResolveDeliveryFile(context.Background(), store, "v1", "tidb.tar.gz")
	if err != nil {
		t.Fatal(err)
	}
	other := layer
	other.Digest = digest.FromString("different")
	pushManifest(t, store, []ocispec.Descriptor{other})
	if manifest.Digest != first.Digest || blob.Digest != layer.Digest {
		t.Fatal("pin changed with tag")
	}
	_, changed, err := ResolveDeliveryFile(context.Background(), store, "v1", "tidb.tar.gz")
	if err != nil || changed.Digest == blob.Digest {
		t.Fatal("test did not move tag")
	}
	pushManifest(t, store, []ocispec.Descriptor{layer, other})
	if _, _, err = ResolveDeliveryFile(context.Background(), store, "v1", "tidb.tar.gz"); err == nil {
		t.Fatal("ambiguous title accepted")
	}
	if _, _, err = ResolveDeliveryFile(context.Background(), store, "v1", "other"); err == nil {
		t.Fatal("missing title accepted")
	}
	layer.URLs = []string{"https://evil.example.com/blob"}
	pushManifest(t, store, []ocispec.Descriptor{layer})
	if _, _, err = ResolveDeliveryFile(context.Background(), store, "v1", "tidb.tar.gz"); err == nil {
		t.Fatal("external descriptor accepted")
	}
}

func TestFetchAuthorizedBlobVerifiesStream(t *testing.T) {
	for _, tt := range []struct {
		name, body string
		wantErr    bool
	}{
		{"valid", "original", false}, {"wrong digest", "modified", true}, {"truncated", "short", true}, {"oversized", "original-extra", true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			desc := content.NewDescriptorFromBytes("application/octet-stream", []byte("original"))
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v2/releases/blobs/"+desc.Digest.String() {
					t.Errorf("unexpected request: %s", r.URL.Path)
				}
				w.Header().Set("Content-Type", desc.MediaType)
				// No Content-Length: verify the streaming reader itself, including trailing data.
				w.(http.Flusher).Flush()
				_, _ = io.WriteString(w, tt.body)
			}))
			defer server.Close()
			repo, err := remote.NewRepository(strings.TrimPrefix(server.URL, "http://") + "/releases")
			if err != nil {
				t.Fatal(err)
			}
			repo.PlainHTTP = true
			rc, err := FetchAuthorizedBlob(context.Background(), repo, desc)
			if err != nil {
				t.Fatal(err)
			}
			defer rc.Close()
			_, err = io.ReadAll(rc)
			if (err != nil) != tt.wantErr {
				t.Fatalf("stream error=%v wantErr=%v", err, tt.wantErr)
			}
		})
	}
}
