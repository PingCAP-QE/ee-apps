package chart_test

import (
	"errors"
	"io"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// Run from the Go module: go test ../charts/github-token-broker/tests/render_test.go
func render(t *testing.T, settings ...string) ([]map[string]any, error) {
	t.Helper()
	_, source, _, _ := runtime.Caller(0)
	chart := filepath.Dir(filepath.Dir(source))
	args := []string{"template", "broker", chart, "--namespace", "ci"}
	for _, setting := range settings {
		args = append(args, "--set", setting)
	}
	output, err := exec.Command("helm", args...).CombinedOutput()
	if err != nil {
		return nil, errors.New(string(output))
	}
	decoder := yaml.NewDecoder(strings.NewReader(string(output)))
	var objects []map[string]any
	for {
		var object map[string]any
		if err := decoder.Decode(&object); err != nil {
			if err == io.EOF {
				break
			}
			t.Fatal(err)
		}
		if object != nil {
			objects = append(objects, object)
		}
	}
	return objects, nil
}

func object(t *testing.T, objects []map[string]any, kind string) map[string]any {
	t.Helper()
	for _, item := range objects {
		if item["kind"] == kind {
			return item
		}
	}
	t.Fatalf("missing %s", kind)
	return nil
}

func mapping(value any, keys ...string) map[string]any {
	for _, key := range keys {
		value = value.(map[string]any)[key]
	}
	return value.(map[string]any)
}

func TestRenderHTTPAndTLS(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		name := "HTTP default"
		var settings []string
		if enabled {
			name = "TLS custom keys and ports"
			settings = []string{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "server.tls.certKey=certificate", "server.tls.keyKey=privateKey", "server.tls.port=9443", "service.httpsPort=444", "serviceMonitor.enabled=true"}
		}
		t.Run(name, func(t *testing.T) {
			objects, err := render(t, settings...)
			if err != nil {
				t.Fatal(err)
			}
			var cfg map[string]any
			if err := yaml.Unmarshal([]byte(mapping(object(t, objects, "ConfigMap"), "data")["config.yaml"].(string)), &cfg); err != nil {
				t.Fatal(err)
			}
			server := mapping(cfg, "server")
			if server["address"] != ":8080" {
				t.Fatalf("HTTP address changed: %v", server)
			}
			spec := mapping(object(t, objects, "Deployment"), "spec", "template", "spec")
			container := spec["containers"].([]any)[0].(map[string]any)
			for _, probe := range []string{"livenessProbe", "readinessProbe"} {
				if mapping(container, probe, "httpGet")["port"] != "http" {
					t.Fatalf("%s changed", probe)
				}
			}
			service := mapping(object(t, objects, "Service"), "spec")
			servicePorts := service["ports"].([]any)
			if mapping(servicePorts[0])["name"] != "http" || mapping(servicePorts[0])["port"] != 80 {
				t.Fatal("HTTP service changed")
			}
			containerPorts := container["ports"].([]any)
			policyPorts := mapping(object(t, objects, "NetworkPolicy"), "spec")["ingress"].([]any)[0].(map[string]any)["ports"].([]any)
			var tlsVolume, tlsMount map[string]any
			for _, volume := range spec["volumes"].([]any) {
				if mapping(volume)["name"] == "tls" {
					tlsVolume = mapping(volume)
				}
			}
			for _, mount := range container["volumeMounts"].([]any) {
				if mapping(mount)["name"] == "tls" {
					tlsMount = mapping(mount)
				}
			}
			if !enabled {
				if server["tls"] != nil || tlsVolume != nil || tlsMount != nil || len(servicePorts) != 1 || len(containerPorts) != 1 || len(policyPorts) != 1 {
					t.Fatal("default render unexpectedly enables TLS")
				}
				return
			}
			tls := mapping(server, "tls")
			if tls["enabled"] != true || tls["address"] != ":9443" || tls["cert_file"] != "/etc/github-token-broker/tls/tls.crt" || tls["key_file"] != "/etc/github-token-broker/tls/tls.key" {
				t.Fatalf("bad TLS config: %v", tls)
			}
			if len(servicePorts) != 2 || mapping(servicePorts[1])["name"] != "https" || mapping(servicePorts[1])["targetPort"] != "https" || mapping(servicePorts[1])["port"] != 444 {
				t.Fatalf("bad service ports: %v", servicePorts)
			}
			if len(containerPorts) != 2 || mapping(containerPorts[1])["name"] != "https" || mapping(containerPorts[1])["containerPort"] != 9443 || len(policyPorts) != 2 || mapping(policyPorts[1])["port"] != 9443 {
				t.Fatal("TLS container/network policy port mismatch")
			}
			if tlsMount == nil || tlsMount["mountPath"] != "/etc/github-token-broker/tls" || tlsMount["readOnly"] != true || tlsMount["subPath"] != nil {
				t.Fatalf("bad TLS directory mount: %v", tlsMount)
			}
			secret := mapping(tlsVolume, "secret")
			items := secret["items"].([]any)
			if secret["secretName"] != "broker-tls" || len(items) != 2 || mapping(items[0])["key"] != "certificate" || mapping(items[0])["path"] != "tls.crt" || mapping(items[1])["key"] != "privateKey" || mapping(items[1])["path"] != "tls.key" {
				t.Fatalf("bad TLS Secret projection: %v", secret)
			}
			endpoint := mapping(object(t, objects, "ServiceMonitor"), "spec")["endpoints"].([]any)[0]
			if mapping(endpoint)["port"] != "http" {
				t.Fatal("ServiceMonitor changed")
			}
		})
	}
}

func TestInvalidHTTPAddress(t *testing.T) {
	for _, enabled := range []string{"false", "true"} {
		t.Run("TLS enabled="+enabled, func(t *testing.T) {
			_, err := render(t, "server.address=:8443", "server.tls.enabled="+enabled, "server.tls.existingSecret=broker-tls")
			if err == nil || !strings.Contains(err.Error(), "server.address must be :8080") {
				t.Fatalf("expected unsupported HTTP address error, got %v", err)
			}
		})
	}
}

func TestInvalidTLSValues(t *testing.T) {
	for _, settings := range [][]string{
		{"server.tls.enabled=true"},
		{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "server.tls.certKey="},
		{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "server.tls.keyKey="},
		{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "server.tls.port=8080"},
		{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "server.tls.port=0"},
		{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "server.tls.port=443"},
		{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "server.tls.port=65536"},
		{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "server.tls.port=not-a-port"},
		{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "service.httpsPort=80"},
		{"server.tls.enabled=true", "server.tls.existingSecret=broker-tls", "service.httpsPort=0"},
	} {
		t.Run(strings.Join(settings, ","), func(t *testing.T) {
			if _, err := render(t, settings...); err == nil {
				t.Fatal("expected Helm rendering failure")
			}
		})
	}
}
