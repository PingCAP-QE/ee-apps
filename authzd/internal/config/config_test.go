package config

import (
	"encoding/json"
	"testing"
)

func TestLoadRequiresPinnedOpenFGAAndCredentialConfiguration(t *testing.T) {
	base := map[string]string{
		"AUTHZD_REGISTRY_PATH":    "/etc/authzd/registry.json",
		"AUTHZD_OPENFGA_URL":      "http://openfga:8080",
		"AUTHZD_OPENFGA_STORE_ID": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
		"AUTHZD_OPENFGA_MODEL_ID": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
		"AUTHZD_CREDENTIAL_MODES": `{"machine":{"header":"authorization","jwks_url":"https://issuer.example/jwks","issuer":"https://issuer.example","audience":"authzd","principal_prefix":"workload:gcp/project/"}}`,
	}
	get := func(name string) string { return base[name] }
	cfg, err := Load(get)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.GRPCAddress != ":9002" || cfg.PrivateAddress != "127.0.0.1:9003" {
		t.Fatalf("default listener addresses = %q/%q", cfg.GRPCAddress, cfg.PrivateAddress)
	}
	delete(base, "AUTHZD_OPENFGA_MODEL_ID")
	if _, err := Load(get); err == nil {
		t.Fatal("Load() accepted an unpinned OpenFGA model")
	}
}

func TestLoadRejectsMalformedCredentialModes(t *testing.T) {
	values := map[string]string{
		"AUTHZD_REGISTRY_PATH":    "/etc/authzd/registry.json",
		"AUTHZD_OPENFGA_URL":      "http://openfga:8080",
		"AUTHZD_OPENFGA_STORE_ID": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
		"AUTHZD_OPENFGA_MODEL_ID": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
		"AUTHZD_CREDENTIAL_MODES": `{"machine":{"header":"bad header","jwks_url":"https://issuer.example/jwks","issuer":"https://issuer.example","audience":"authzd","principal_prefix":"workload:gcp/project/"}}`,
	}
	if _, err := Load(func(name string) string { return values[name] }); err == nil {
		t.Fatal("Load() accepted invalid credential header")
	}
	values["AUTHZD_CREDENTIAL_MODES"] = `{"machine":{"header":"x-user-id-token","jwks_url":"https://issuer.example/jwks","issuer":"https://issuer.example","audience":"authzd","principal_prefix":"workload:gcp/project/"}}`
	if _, err := Load(func(name string) string { return values[name] }); err == nil {
		t.Fatal("Load() accepted an identity claim header as a token header")
	}
}

func TestLoadRejectsUnknownCredentialConfigurationFields(t *testing.T) {
	values := map[string]string{
		"AUTHZD_REGISTRY_PATH":    "/etc/authzd/registry.json",
		"AUTHZD_OPENFGA_URL":      "http://openfga:8080",
		"AUTHZD_OPENFGA_STORE_ID": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
		"AUTHZD_OPENFGA_MODEL_ID": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
		"AUTHZD_CREDENTIAL_MODES": `{"machine":{"header":"authorization","jwks_url":"https://issuer.example/jwks","issuer":"https://issuer.example","audience":"authzd","principal_prefix":"workload:gcp/project/","allow_unverified":true}}`,
	}
	if _, err := Load(func(name string) string { return values[name] }); err == nil {
		t.Fatal("Load() accepted an unknown credential security field")
	}
}

func TestBrowserAndMachineTrustDomains(t *testing.T) {
	browser := Credential{Header: "x-forwarded-id-token", Issuer: "https://accounts.google.com", Audience: "browser-client", JWKSURL: "https://example.com/keys", PrincipalPrefix: "user:https://accounts.google.com|"}
	machine := Credential{Header: "authorization", Issuer: browser.Issuer, Audience: "prow.tidb.net", JWKSURL: browser.JWKSURL, PrincipalPrefix: "workload:gcp/"}
	for _, tc := range []struct {
		name   string
		change func(*Credential, *Credential)
		valid  bool
	}{
		{"distinct", func(b, m *Credential) {}, true},
		{"shared header", func(b, m *Credential) { b.Header = "AUTHORIZATION" }, false},
		{"shared token audience", func(b, m *Credential) { m.Audience = b.Audience }, false},
		{"browser uses email", func(b, m *Credential) { b.SubjectClaim = "email" }, false},
		{"browser as workload", func(b, m *Credential) { b.PrincipalPrefix = m.PrincipalPrefix }, false},
		{"machine as user", func(b, m *Credential) { m.PrincipalPrefix = b.PrincipalPrefix }, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b, m := browser, machine
			tc.change(&b, &m)
			raw, err := json.Marshal(map[string]Credential{"browser": b, "machine": m})
			if err != nil {
				t.Fatal(err)
			}
			_, err = Load(func(name string) string {
				if name == "AUTHZD_CREDENTIAL_MODES" {
					return string(raw)
				}
				return "configured"
			})
			if (err == nil) != tc.valid {
				t.Fatalf("Load error = %v, want valid=%t", err, tc.valid)
			}
		})
	}
}
