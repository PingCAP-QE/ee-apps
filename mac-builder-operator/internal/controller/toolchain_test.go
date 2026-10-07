package controller

import (
	"reflect"
	"testing"

	toml "github.com/pelletier/go-toml/v2"
)

func TestResolveMacOSTools(t *testing.T) {
	t.Parallel()

	t.Run("returns tools of the matched builder", func(t *testing.T) {
		t.Parallel()
		data := []byte(`
builders:
  - if: false
    image: ghcr.io/x/pd:go1.25
    macos:
      tools:
        go: "1.25"
  - if: true
    image: ghcr.io/x/pd:go1.23
    macos:
      tools:
        go: "1.23"
        deno: "2"
`)
		got, err := resolveMacOSTools(data)
		if err != nil {
			t.Fatalf("resolveMacOSTools: %v", err)
		}
		if got["go"] != "1.23" || got["deno"] != "2" {
			t.Fatalf("unexpected tools: %#v", got)
		}
		if len(got) != 2 {
			t.Fatalf("expected 2 tools, got %#v", got)
		}
	})

	t.Run("returns nil when the matched builder has no macos.tools", func(t *testing.T) {
		t.Parallel()
		data := []byte(`
builders:
  - if: true
    image: ghcr.io/x/pd:go1.18
`)
		got, err := resolveMacOSTools(data)
		if err != nil {
			t.Fatalf("resolveMacOSTools: %v", err)
		}
		if got != nil {
			t.Fatalf("expected nil tools, got %#v", got)
		}
	})

	t.Run("errors on invalid yaml", func(t *testing.T) {
		t.Parallel()
		if _, err := resolveMacOSTools([]byte("builders: [")); err == nil {
			t.Fatalf("expected error for invalid yaml")
		}
	})
}

func TestRenderMiseToml(t *testing.T) {
	t.Parallel()

	tools := map[string]string{
		"go":                     "1.25",
		"aqua:mikefarah/yq":      "4",
		"aqua:jqlang/jq":         "1.7",
		"aqua:oras-project/oras": "1",
	}
	out, err := renderMiseToml(tools)
	if err != nil {
		t.Fatalf("renderMiseToml: %v", err)
	}

	var parsed struct {
		Tools map[string]string `toml:"tools"`
	}
	if err := toml.Unmarshal([]byte(out), &parsed); err != nil {
		t.Fatalf("unmarshal rendered toml: %v\n%s", err, out)
	}
	if !reflect.DeepEqual(parsed.Tools, tools) {
		t.Fatalf("round-trip mismatch:\n got %#v\nwant %#v", parsed.Tools, tools)
	}

	empty, err := renderMiseToml(nil)
	if err != nil || empty != "" {
		t.Fatalf("expected empty toml for empty tools, got %q (err %v)", empty, err)
	}
}
