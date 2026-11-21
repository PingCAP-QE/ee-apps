package oci

type Config struct {
	Username string `yaml:"username,omitempty" json:"username,omitempty"`
	Password string `yaml:"password,omitempty" json:"password,omitempty"`
	// ArtifactRegistryADC mints a short-lived access token from the Pod's
	// Google identity (Workload Identity / ADC) for registries under
	// `*.pkg.dev`. Use it for Artifact Registry instead of a static key.
	ArtifactRegistryADC bool `yaml:"artifact_registry_adc,omitempty" json:"artifact_registry_adc,omitempty"`
}
