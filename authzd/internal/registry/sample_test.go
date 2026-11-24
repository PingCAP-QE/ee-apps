package registry

import (
	"os"
	"testing"
)

func TestTiBuildSampleRegistryIsValid(t *testing.T) {
	data, err := os.ReadFile("../../policies/registry-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	r, err := Parse(data)
	if err != nil {
		t.Fatalf("parse sample registry: %v", err)
	}
	if r.Version() != "1" {
		t.Fatalf("version = %q, want 1", r.Version())
	}
	if len(r.rules) != 3 {
		t.Fatalf("routes = %d, want 3", len(r.rules))
	}
	for _, rule := range r.Rules() {
		capability, resource := false, false
		for _, check := range rule.Checks {
			capability = capability || check.Object != ""
			resource = resource || check.ObjectFrom != ""
		}
		if !capability || !resource {
			t.Errorf("sample route %s must explicitly include capability and resource checks", rule.ID)
		}
	}
}
