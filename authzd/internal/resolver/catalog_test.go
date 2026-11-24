package resolver

import (
	"context"
	"testing"
)

func TestParseCatalogResolvesOnlyTrustedCanonicalResources(t *testing.T) {
	catalog, err := Parse([]byte(`{"version":"catalog-v1","resources":{"devbuild/17":"devbuild:canonical-17"}}`))
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	got, err := catalog.Resolve(context.Background(), "devbuild", "17")
	if err != nil || got != "devbuild:canonical-17" {
		t.Fatalf("Resolve() = (%q, %v)", got, err)
	}
	if _, err := catalog.Resolve(context.Background(), "devbuild", "18"); err == nil {
		t.Fatal("unknown resource resolved without trusted ownership data")
	}
}

func TestParseCatalogRejectsMismatchedObjectTypeAndUnknownFields(t *testing.T) {
	for _, data := range []string{
		`{"version":"v1","resources":{"project/1":"devbuild:1"}}`,
		`{"version":"v1","resources":{"project/1":"project:1"},"fallback":"allow"}`,
		`{"version":"v1","resources":{"project/":"project:1"}}`,
		`{"version":"","resources":{"project/1":"project:1"}}`,
	} {
		if _, err := Parse([]byte(data)); err == nil {
			t.Fatalf("Parse(%s) succeeded", data)
		}
	}
}

func TestResolveRejectsNilCatalogAndIncompleteKey(t *testing.T) {
	var catalog *StaticCatalog
	if _, err := catalog.Resolve(context.Background(), "devbuild", "1"); err == nil {
		t.Fatal("nil catalog resolved a resource")
	}
	if _, err := catalog.Resolve(context.Background(), "", ""); err == nil {
		t.Fatal("catalog resolved an incomplete resource key")
	}
}
