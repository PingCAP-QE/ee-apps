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

// TestResolveNeverCoercesIDSpellings locks the deny-by-default behavior for
// path and query spellings that a client could use to reach another resource.
// A catalog lookup is exact: no case folding, whitespace trimming, zero
// padding, slash acceptance, or percent-decoding happens here.
func TestResolveNeverCoercesIDSpellings(t *testing.T) {
	catalog, err := Parse([]byte(`{"version":"catalog-v1","resources":{"devbuild/17":"devbuild:canonical-17"}}`))
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	for _, id := range []string{"17 ", " 17", "017", "0017", "017x", "DevBuild/17", "devbuild/17", "%31%37", "1%37", "17\n", "17\x00"} {
		if object, err := catalog.Resolve(context.Background(), "devbuild", id); err == nil {
			t.Errorf("Resolve(%q) = %q, want no resolution for a non-canonical ID", id, object)
		}
	}
}

// TestResolveIsScopedByResourceType documents that the same ID under a
// different type is a different object and does not inherit ownership.
func TestResolveIsScopedByResourceType(t *testing.T) {
	catalog, err := Parse([]byte(`{"version":"catalog-v1","resources":{"devbuild/17":"devbuild:canonical-17"}}`))
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	for _, resourceType := range []string{"project", "Devbuild", "devbuilds", ""} {
		if object, err := catalog.Resolve(context.Background(), resourceType, "17"); err == nil {
			t.Errorf("Resolve(%q, 17) = %q, want no resolution", resourceType, object)
		}
	}
}

func TestUnavailableResolverFailsClosed(t *testing.T) {
	var unavailable Unavailable
	if _, err := unavailable.Resolve(context.Background(), "devbuild", "17"); err == nil {
		t.Fatal("unavailable resolver resolved a resource")
	}
	if unavailable.Version() != "unconfigured" {
		t.Fatalf("Version() = %q", unavailable.Version())
	}
}

func TestCatalogVersionIsExposed(t *testing.T) {
	catalog, err := Parse([]byte(`{"version":"catalog-v2","resources":{"devbuild/17":"devbuild:17"}}`))
	if err != nil {
		t.Fatal(err)
	}
	if catalog.Version() != "catalog-v2" {
		t.Fatalf("Version() = %q", catalog.Version())
	}
}

func TestParseRejectsNonIdentifierNamesAndIDs(t *testing.T) {
	for _, data := range []string{
		`{"version":"v1","resources":{"dev build/17":"dev build:17"}}`,
		`{"version":"v1","resources":{"9devbuild/17":"9devbuild:17"}}`,
		`{"version":"v1","resources":{"devbuild/17":"devbuild:17 18"}}`,
		`{"version":"v1","resources":{"devbuild/17":"devbuild:17\n18"}}`,
		`{"version":"v1","resources":{"devbuild/a/b":"devbuild:a/b"}}`,
		`{"version":"v1","resources":{"devbuild/17":"devbuild"}}`,
	} {
		if _, err := Parse([]byte(data)); err == nil {
			t.Errorf("Parse(%s) accepted a non-identifier resource entry", data)
		}
	}
}
