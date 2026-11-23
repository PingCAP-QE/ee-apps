// Package secure seals one-time download URLs at rest with AES-256-GCM.
package secure

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
)

const keySize = 32

type Sealer struct {
	aead    cipher.AEAD
	version byte
}

// ParseKey accepts a base64-encoded 32-byte key in standard or URL-safe form.
func ParseKey(encoded string) ([]byte, error) {
	for _, enc := range []*base64.Encoding{
		base64.StdEncoding, base64.RawStdEncoding,
		base64.URLEncoding, base64.RawURLEncoding,
	} {
		if key, err := enc.DecodeString(encoded); err == nil && len(key) == keySize {
			return key, nil
		}
	}
	return nil, errors.New("URL encryption key must be 32 base64-encoded bytes")
}

func New(key []byte) (*Sealer, error) {
	if len(key) != keySize {
		return nil, fmt.Errorf("URL encryption key must be %d bytes", keySize)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return &Sealer{aead: aead, version: 1}, nil
}

// Seal returns version || nonce || ciphertext. The plaintext is never stored.
func (s *Sealer) Seal(plaintext string) ([]byte, error) {
	nonce := make([]byte, s.aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	out := make([]byte, 0, 1+len(nonce)+len(plaintext)+s.aead.Overhead())
	out = append(out, s.version)
	out = append(out, nonce...)
	return s.aead.Seal(out, nonce, []byte(plaintext), nil), nil
}

func (s *Sealer) Open(ciphertext []byte) (string, error) {
	if len(ciphertext) < 1+s.aead.NonceSize()+s.aead.Overhead() {
		return "", errors.New("sealed URL too short")
	}
	if ciphertext[0] != s.version {
		return "", errors.New("unsupported sealed URL version")
	}
	nonce := ciphertext[1 : 1+s.aead.NonceSize()]
	plaintext, err := s.aead.Open(nil, nonce, ciphertext[1+s.aead.NonceSize():], nil)
	if err != nil {
		return "", errors.New("cannot open sealed URL")
	}
	return string(plaintext), nil
}
