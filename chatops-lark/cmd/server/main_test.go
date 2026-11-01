package main

import (
	"strings"
	"testing"
)

func TestParseOptionsDefaults(t *testing.T) {
	opts, err := parseOptions(nil)
	if err != nil {
		t.Fatal(err)
	}
	if opts.configPath != "config.yaml" {
		t.Fatalf("configPath = %q, want config.yaml", opts.configPath)
	}
	if opts.httpAddress != ":8080" {
		t.Fatalf("httpAddress = %q, want :8080", opts.httpAddress)
	}
	if opts.debug {
		t.Fatal("debug = true, want false")
	}
}

func TestParseOptionsConfigPath(t *testing.T) {
	opts, err := parseOptions([]string{"-config", "/etc/chatops-lark/config.yaml", "-debug", "-http-addr", ":9090"})
	if err != nil {
		t.Fatal(err)
	}
	if opts.configPath != "/etc/chatops-lark/config.yaml" {
		t.Fatalf("configPath = %q", opts.configPath)
	}
	if opts.httpAddress != ":9090" {
		t.Fatalf("httpAddress = %q", opts.httpAddress)
	}
	if !opts.debug {
		t.Fatal("debug = false, want true")
	}
}

func TestParseOptionsRejectsAppCredentials(t *testing.T) {
	for _, flagName := range []string{"-app-id", "-app-secret"} {
		t.Run(flagName, func(t *testing.T) {
			_, err := parseOptions([]string{flagName, "value"})
			if err == nil {
				t.Fatalf("%s was accepted; credentials must come from the config file only", flagName)
			}
			if !strings.Contains(err.Error(), "flag provided but not defined") {
				t.Fatalf("error = %v, want flag provided but not defined", err)
			}
		})
	}
}
