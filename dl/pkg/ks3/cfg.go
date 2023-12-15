package ks3

type Config struct {
	Region    string `yaml:"region,omitempty" json:"region,omitempty"`
	Endpoint  string `yaml:"endpoint,omitempty" json:"endpoint,omitempty"`
	AccessKey string `yaml:"access_key,omitempty" json:"access_key,omitempty"`
	SecretKey string `yaml:"secret_key,omitempty" json:"secret_key,omitempty"`
}
