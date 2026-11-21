package oci

import (
	"context"
	"errors"
	"io"
	"log"
	"testing"

	"oras.land/oras-go/v2/registry/remote/auth"
)

type fakeMinter struct {
	cred auth.Credential
	err  error
	used int
}

func (f *fakeMinter) credential(context.Context) (auth.Credential, error) {
	f.used++
	return f.cred, f.err
}

func newTestService(static auth.Credential, adc bool, minter tokenMinter) *ocisrvc {
	return &ocisrvc{
		logger:              log.New(io.Discard, "", 0),
		credential:          &static,
		artifactRegistryADC: adc,
		tokens:              minter,
	}
}

func TestCredentialForStaticWins(t *testing.T) {
	svc := newTestService(auth.Credential{Username: "robot$release", Password: "pw"}, true, &fakeMinter{})
	cred, err := svc.credentialFor(context.Background(), "us-docker.pkg.dev")
	if err != nil {
		t.Fatal(err)
	}
	if cred.Username != "robot$release" || cred.Password != "pw" {
		t.Fatalf("static credential not used: %+v", cred)
	}
}

func TestCredentialForAnonymousWhenDisabled(t *testing.T) {
	svc := newTestService(auth.EmptyCredential, false, &fakeMinter{})
	cred, err := svc.credentialFor(context.Background(), "us-docker.pkg.dev")
	if err != nil {
		t.Fatal(err)
	}
	if cred != auth.EmptyCredential {
		t.Fatalf("want anonymous, got %+v", cred)
	}
}

func TestCredentialForArtifactRegistry(t *testing.T) {
	minter := &fakeMinter{cred: auth.Credential{Username: "oauth2accesstoken", Password: "token"}}
	svc := newTestService(auth.EmptyCredential, true, minter)

	cred, err := svc.credentialFor(context.Background(), "us-docker.pkg.dev")
	if err != nil {
		t.Fatal(err)
	}
	if cred.Username != "oauth2accesstoken" || cred.Password != "token" {
		t.Fatalf("want minted token, got %+v", cred)
	}

	// A non-Artifact-Registry host must stay anonymous even with ADC enabled.
	cred, err = svc.credentialFor(context.Background(), "hub.pingcap.net")
	if err != nil {
		t.Fatal(err)
	}
	if cred != auth.EmptyCredential {
		t.Fatalf("want anonymous for other hosts, got %+v", cred)
	}
	if minter.used != 1 {
		t.Fatalf("minter used %d times, want 1", minter.used)
	}
}

func TestCredentialForArtifactRegistryFallsBackToAnonymous(t *testing.T) {
	minter := &fakeMinter{err: errors.New("no identity")}
	svc := newTestService(auth.EmptyCredential, true, minter)

	cred, err := svc.credentialFor(context.Background(), "us-docker.pkg.dev")
	if err != nil {
		t.Fatalf("a broken identity must not fail the request: %v", err)
	}
	if cred != auth.EmptyCredential {
		t.Fatalf("want anonymous fallback, got %+v", cred)
	}
}

func TestIsArtifactRegistry(t *testing.T) {
	for _, tt := range []struct {
		host string
		want bool
	}{
		{"us-docker.pkg.dev", true},
		{"asia-northeast1-docker.pkg.dev", true},
		{"US-DOCKER.PKG.DEV", true},
		{"us-docker.pkg.dev.", true},
		{"pkg.dev.example.com", false},
		{"hub.pingcap.net", false},
		{"pkg.dev", false},
	} {
		if got := isArtifactRegistry(tt.host); got != tt.want {
			t.Errorf("isArtifactRegistry(%q) = %v, want %v", tt.host, got, tt.want)
		}
	}
}
