package registry

import (
	"encoding/json"
	"testing"
)

func rule(id, method, routePath string, priority int) Rule {
	return Rule{
		ID: id, Host: "prow.tidb.net", Route: "tibuild-v2", Method: method,
		Path: routePath, Priority: priority, Mode: "observe",
		Checks: []Check{{Object: "capability:tibuild.devbuild.rerun", Relation: "invoke"}, {ObjectFrom: "devbuild", IDFrom: "query.id", Relation: "can_rerun"}},
	}
}

func config(rules ...Rule) []byte {
	data, _ := json.Marshal(Config{Version: "7", Routes: rules})
	return data
}

func TestMatchRequiresExactMethodRouteAndPath(t *testing.T) {
	r, err := Parse(config(rule("rerun", "POST", "/tibuild/api/v2/devbuilds/{id}/rerun", 10)))
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name    string
		request Request
		want    bool
	}{
		{"exact", Request{Host: "PROW.TIDB.NET:443", Route: "tibuild-v2", Method: "post", ExternalPath: "/tibuild/api/v2/devbuilds/42/rerun"}, true},
		{"wrong method", Request{Host: "prow.tidb.net", Route: "tibuild-v2", Method: "GET", ExternalPath: "/tibuild/api/v2/devbuilds/42/rerun"}, false},
		{"wrong route", Request{Host: "prow.tidb.net", Route: "other", Method: "POST", ExternalPath: "/tibuild/api/v2/devbuilds/42/rerun"}, false},
		{"prefix sibling", Request{Host: "prow.tidb.net", Route: "tibuild-v2", Method: "POST", ExternalPath: "/tibuild/api/v2/devbuilds/42/rerun/extra"}, false},
		{"encoded path", Request{Host: "prow.tidb.net", Route: "tibuild-v2", Method: "POST", ExternalPath: "/tibuild/api/v2/devbuilds/a%2fb/rerun"}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, got := r.Match(tt.request)
			if got != tt.want {
				t.Fatalf("Match() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestMatchNormalizedPathTemplateAndUpstreamRewrite(t *testing.T) {
	r, err := Parse(config(rule("rewritten", "POST", "/api/v2/devbuilds/{id}/rerun", 1)))
	if err != nil {
		t.Fatal(err)
	}
	got, ok := r.Match(Request{Host: "prow.tidb.net", Route: "tibuild-v2", Method: "POST", ExternalPath: "/tibuild/api/v2/devbuilds/abc/rerun", UpstreamPath: "/api/v2/devbuilds/abc/rerun"})
	if !ok || got.MatchedOn != "upstream" || got.PathValues["id"] != "abc" {
		t.Fatalf("unexpected match: %#v, %v", got, ok)
	}
	if _, err := Parse(config(rule("bad-normalization", "POST", "/api//devbuilds/{id}/rerun", 1))); err == nil {
		t.Fatal("expected non-normalized template to fail")
	}
}

func TestPriorityResolvesOverlappingTemplates(t *testing.T) {
	lessSpecific := rule("generic", "POST", "/devbuilds/{id}/rerun", 1)
	moreSpecific := rule("specific", "POST", "/devbuilds/fixed/rerun", 1)
	r, err := Parse(config(lessSpecific, moreSpecific))
	if err != nil {
		t.Fatal(err)
	}
	got, ok := r.Match(Request{Host: "prow.tidb.net", Route: "tibuild-v2", Method: "POST", ExternalPath: "/devbuilds/fixed/rerun"})
	if !ok || got.Rule.ID != "specific" {
		t.Fatalf("selected %q, want specific", got.Rule.ID)
	}
	lessSpecific.Priority = 2
	r, err = Parse(config(lessSpecific, moreSpecific))
	if err != nil {
		t.Fatal(err)
	}
	got, _ = r.Match(Request{Host: "prow.tidb.net", Route: "tibuild-v2", Method: "POST", ExternalPath: "/devbuilds/fixed/rerun"})
	if got.Rule.ID != "generic" {
		t.Fatalf("priority selected %q, want generic", got.Rule.ID)
	}
}

func TestRejectsAmbiguousConflictAndMissingChecks(t *testing.T) {
	a, b := rule("one", "POST", "/devbuilds/{id}/rerun", 1), rule("two", "POST", "/devbuilds/{name}/rerun", 1)
	if _, err := Parse(config(a, b)); err == nil {
		t.Fatal("expected ambiguous template conflict")
	}
	a.Checks = nil
	if _, err := Parse(config(a)); err == nil {
		t.Fatal("expected missing checks to fail closed")
	}
}

func TestRejectsObjectNamingConflictsAndIncompleteDualCheck(t *testing.T) {
	r := rule("bad-object", "POST", "/rerun", 1)
	r.Checks = append(r.Checks, Check{Object: "capability:tibuild.devbuild.rerun", Relation: "read"})
	if _, err := Parse(config(r)); err == nil {
		t.Fatal("expected capability object relation conflict")
	}
	r = rule("missing-resource", "POST", "/rerun", 1)
	r.Checks = r.Checks[:1]
	parsed, err := Parse(config(r))
	if err != nil || len(parsed.rules[0].Checks) != 1 {
		t.Fatalf("single explicit capability check should parse: %v", err)
	}
	r.Checks = []Check{{ObjectFrom: "devbuild", Relation: "can_rerun"}}
	if _, err := Parse(config(r)); err == nil {
		t.Fatal("expected incomplete dynamic resource check to fail")
	}
}

func TestRejectsCapabilityObjectReusedForDifferentRelationAcrossRoutes(t *testing.T) {
	a := rule("rerun", "POST", "/rerun", 1)
	b := rule("read", "GET", "/read", 1)
	b.Checks[0].Relation = "read"
	if _, err := Parse(config(a, b)); err == nil {
		t.Fatal("expected global object naming conflict")
	}
}

func TestStoreReplacesAtomicallyOnlyAfterValidation(t *testing.T) {
	store := new(Store)
	first := config(rule("first", "POST", "/one", 1))
	if err := store.Replace(first); err != nil {
		t.Fatal(err)
	}
	previous := store.Load()
	if err := store.Replace([]byte(`{"version":"bad","routes":[]} trailing`)); err == nil {
		t.Fatal("expected invalid replacement")
	}
	if store.Load() != previous {
		t.Fatal("invalid replacement changed active snapshot")
	}
	if err := store.Replace(config(rule("second", "GET", "/two", 1))); err != nil {
		t.Fatal(err)
	}
	if store.Load().Version() != "7" {
		t.Fatalf("version = %q", store.Load().Version())
	}
}
