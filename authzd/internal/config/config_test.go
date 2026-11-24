package config

import (
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
