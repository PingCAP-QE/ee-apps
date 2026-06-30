package gcs

type Config struct {
	CredentialsFile string `yaml:"credentials_file,omitempty" json:"credentials_file,omitempty"`
	CredentialsJSON string `yaml:"credentials_json,omitempty" json:"credentials_json,omitempty"`
}
