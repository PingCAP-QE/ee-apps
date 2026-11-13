package ks3

type Config struct {
	Region    string `yaml:"region,omitempty" json:"region,omitempty"`
	Endpoint  string `yaml:"endpoint,omitempty" json:"endpoint,omitempty"`
	AccessKey string `yaml:"access_key,omitempty" json:"access_key,omitempty"`
	SecretKey string `yaml:"secret_key,omitempty" json:"secret_key,omitempty"`

	// DisableSSL sends every credentialed request over plaintext. It exists for
	// an endpoint that only speaks HTTP; anything else leaves TLS on, which is
	// the default and what the TLS-verification finding is about.
	DisableSSL bool `yaml:"disable_ssl,omitempty" json:"disable_ssl,omitempty"`

	// AllowedBuckets lists the buckets a download may name, matched exactly and
	// case-insensitively. The request supplies the bucket and the service reads
	// it with its own credentials, so an unset list rejects every bucket rather
	// than allowing any.
	AllowedBuckets []string `yaml:"allowed_buckets,omitempty" json:"allowed_buckets,omitempty"`

	// AllowedKeyPrefixes optionally narrows the keys inside those buckets. Empty
	// means "any key of an allowed bucket"; a non-empty list rejects a key that
	// matches none of the prefixes.
	AllowedKeyPrefixes []string `yaml:"allowed_key_prefixes,omitempty" json:"allowed_key_prefixes,omitempty"`
}
