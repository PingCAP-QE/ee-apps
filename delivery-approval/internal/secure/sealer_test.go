package secure

import (
	"encoding/base64"
	"strings"
	"testing"
)

func testKey() []byte {
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(i)
	}
	return key
}

func TestSealRoundTrip(t *testing.T) {
	s, err := New(testKey())
	if err != nil {
		t.Fatal(err)
	}
	plaintext := "https://dl.pingcap.net/d/abcdef"
	sealed, err := s.Seal(plaintext)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(sealed), plaintext) {
		t.Fatal("plaintext leaked into ciphertext")
	}
	got, err := s.Open(sealed)
	if err != nil || got != plaintext {
		t.Fatalf("open = %q, %v", got, err)
	}
}

func TestSealRejectsWrongKeyAndTamper(t *testing.T) {
	s, _ := New(testKey())
	sealed, _ := s.Seal("https://example.com/d/x")

	otherKey := testKey()
	otherKey[0] ^= 0xff
	other, _ := New(otherKey)
	if _, err := other.Open(sealed); err == nil {
		t.Fatal("wrong key opened the URL")
	}

	sealed[len(sealed)-1] ^= 0xff
	if _, err := s.Open(sealed); err == nil {
		t.Fatal("tampered ciphertext opened")
	}
	if _, err := s.Open([]byte{1, 2}); err == nil {
		t.Fatal("short ciphertext opened")
	}
}

func TestParseKey(t *testing.T) {
	key := testKey()
	for _, enc := range []*base64.Encoding{base64.StdEncoding, base64.RawStdEncoding, base64.URLEncoding, base64.RawURLEncoding} {
		got, err := ParseKey(enc.EncodeToString(key))
		if err != nil || len(got) != 32 {
			t.Fatalf("parse %v: %v", enc, err)
		}
	}
	if _, err := ParseKey("not-base64"); err == nil {
		t.Fatal("invalid key accepted")
	}
	if _, err := ParseKey(base64.StdEncoding.EncodeToString([]byte("short"))); err == nil {
		t.Fatal("short key accepted")
	}
}
