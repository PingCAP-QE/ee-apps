package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
)

type Credential struct {
	Header          string `json:"header"`
	JWKSURL         string `json:"jwks_url"`
	Issuer          string `json:"issuer"`
	Audience        string `json:"audience"`
	PrincipalPrefix string `json:"principal_prefix"`
	SubjectClaim    string `json:"subject_claim,omitempty"`
}

type Config struct {
	GRPCAddress     string
	PrivateAddress  string
	RegistryPath    string
	ResourceCatalog string
	OpenFGAURL      string
	OpenFGAStoreID  string
	OpenFGAModelID  string
	OpenFGAAPIToken string
	Credentials     map[string]Credential
}

func Load(getenv func(string) string) (Config, error) {
	cfg := Config{
		GRPCAddress:     defaultValue(getenv("AUTHZD_GRPC_ADDR"), ":9002"),
		PrivateAddress:  defaultValue(getenv("AUTHZD_PRIVATE_ADDR"), "127.0.0.1:9003"),
		RegistryPath:    getenv("AUTHZD_REGISTRY_PATH"),
		ResourceCatalog: getenv("AUTHZD_RESOURCE_CATALOG"),
		OpenFGAURL:      getenv("AUTHZD_OPENFGA_URL"),
		OpenFGAStoreID:  getenv("AUTHZD_OPENFGA_STORE_ID"),
		OpenFGAModelID:  getenv("AUTHZD_OPENFGA_MODEL_ID"),
		OpenFGAAPIToken: getenv("AUTHZD_OPENFGA_API_TOKEN"),
	}
	credentialsJSON := getenv("AUTHZD_CREDENTIAL_MODES")
	if strings.TrimSpace(credentialsJSON) != "" {
		decoder := json.NewDecoder(bytes.NewBufferString(credentialsJSON))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&cfg.Credentials); err != nil {
			return Config{}, errors.New("AUTHZD_CREDENTIAL_MODES must be a JSON object")
		}
		if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
			return Config{}, errors.New("AUTHZD_CREDENTIAL_MODES must contain one JSON object")
		}
	}
	if cfg.RegistryPath == "" || cfg.OpenFGAURL == "" || cfg.OpenFGAStoreID == "" || cfg.OpenFGAModelID == "" || len(cfg.Credentials) == 0 {
		return Config{}, errors.New("registry, OpenFGA endpoint/store/model, and credential modes are required")
	}
	for mode, credential := range cfg.Credentials {
		if strings.TrimSpace(mode) == "" || !validCredentialHeader(credential.Header) || credential.JWKSURL == "" || credential.Issuer == "" || credential.Audience == "" || credential.PrincipalPrefix == "" {
			return Config{}, fmt.Errorf("credential mode %q is incomplete", mode)
		}
	}
	return cfg, nil
}

func validCredentialHeader(header string) bool {
	lower := strings.ToLower(header)
	if !validHeader(header) || strings.HasPrefix(lower, "x-user-") || strings.HasPrefix(lower, "x-tenant-") {
		return false
	}
	return true
}

func validHeader(header string) bool {
	if header == "" {
		return false
	}
	for _, r := range header {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || strings.ContainsRune("!#$%&'*+-.^_`|~", r) {
			continue
		}
		return false
	}
	return true
}

func defaultValue(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
