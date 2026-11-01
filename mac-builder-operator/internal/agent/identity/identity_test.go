package identity

import (
	"context"
	"errors"
	"strings"
	"testing"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const testHostname = "mac-mini-01"

type fakeRunner struct {
	outputs map[string]string
	errs    map[string]error
}

func (f fakeRunner) Run(_ context.Context, name string, args ...string) (string, error) {
	key := strings.TrimSpace(name + " " + strings.Join(args, " "))
	if err, ok := f.errs[key]; ok {
		return "", err
	}
	if out, ok := f.outputs[key]; ok {
		return out, nil
	}
	return "", errors.New("command not found: " + key)
}

func TestResolvePlatformUUID(t *testing.T) {
	t.Parallel()

	runner := fakeRunner{outputs: map[string]string{
		"ioreg -rd1 -c IOPlatformExpertDevice": `    "IOPlatformUUID" = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"`,
	}}
	resolver := Resolver{
		Runner:   runner,
		Hostname: func() (string, error) { return testHostname, nil },
	}
	got, err := resolver.Resolve(context.Background())
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if got.Name != "mac-3f2504e0-4f89-11d3-9a0c-0305e82c3301" {
		t.Fatalf("unexpected name: %q", got.Name)
	}
	if got.Source != buildv1alpha1.MacAgentMachineIDSourcePlatformUUID {
		t.Fatalf("unexpected source: %q", got.Source)
	}
	if got.Value != "3F2504E0-4F89-11D3-9A0C-0305E82C3301" {
		t.Fatalf("unexpected value: %q", got.Value)
	}
}

func TestResolveSerialFallback(t *testing.T) {
	t.Parallel()

	runner := fakeRunner{
		outputs: map[string]string{
			"system_profiler SPHardwareDataType": "Hardware:\n\n    Hardware Overview:\n\n      Model Name: Mac mini\n      Serial Number (system): C02XYZ123456\n",
		},
		errs: map[string]error{
			"ioreg -rd1 -c IOPlatformExpertDevice": errors.New("ioreg unavailable"),
		},
	}
	resolver := Resolver{
		Runner:   runner,
		Hostname: func() (string, error) { return testHostname, nil },
	}
	got, err := resolver.Resolve(context.Background())
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if got.Name != "mac-serial-c02xyz123456" {
		t.Fatalf("unexpected name: %q", got.Name)
	}
	if got.Source != buildv1alpha1.MacAgentMachineIDSourceSerial {
		t.Fatalf("unexpected source: %q", got.Source)
	}
}

func TestResolveHostnameFallback(t *testing.T) {
	t.Parallel()

	runner := fakeRunner{errs: map[string]error{
		"ioreg -rd1 -c IOPlatformExpertDevice": errors.New("ioreg unavailable"),
		"system_profiler SPHardwareDataType":   errors.New("system_profiler unavailable"),
	}}
	resolver := Resolver{
		Runner:   runner,
		Hostname: func() (string, error) { return "Mac-Mini-01.local", nil },
	}
	got, err := resolver.Resolve(context.Background())
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if got.Name != NameFromHostname("Mac-Mini-01.local") {
		t.Fatalf("unexpected name: %q", got.Name)
	}
	if got.Source != buildv1alpha1.MacAgentMachineIDSourceHostname {
		t.Fatalf("unexpected source: %q", got.Source)
	}
}

func TestResolveOverride(t *testing.T) {
	t.Parallel()

	runner := fakeRunner{outputs: map[string]string{
		"ioreg -rd1 -c IOPlatformExpertDevice": `"IOPlatformUUID" = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"`,
	}}
	resolver := Resolver{
		Runner:   runner,
		Override: " Custom-Agent ",
		Hostname: func() (string, error) { return testHostname, nil },
	}
	got, err := resolver.Resolve(context.Background())
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if got.Name != "custom-agent" {
		t.Fatalf("unexpected override name: %q", got.Name)
	}
	if got.Source != buildv1alpha1.MacAgentMachineIDSourcePlatformUUID {
		t.Fatalf("unexpected source: %q", got.Source)
	}
}

func TestResolveFailsWithoutIdentity(t *testing.T) {
	t.Parallel()

	runner := fakeRunner{errs: map[string]error{
		"ioreg -rd1 -c IOPlatformExpertDevice": errors.New("ioreg unavailable"),
		"system_profiler SPHardwareDataType":   errors.New("system_profiler unavailable"),
	}}
	resolver := Resolver{
		Runner:   runner,
		Hostname: func() (string, error) { return "", errors.New("hostname unavailable") },
	}
	if _, err := resolver.Resolve(context.Background()); err == nil {
		t.Fatal("expected an error when no identity source is available")
	}
}

func TestParseIOPlatformUUID(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name  string
		input string
		want  string
	}{
		{"quoted", `"IOPlatformUUID" = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"`, "3F2504E0-4F89-11D3-9A0C-0305E82C3301"},
		{"noise", "  | |   \"IOPlatformUUID\" = \"abc-def\"  \n", "abc-def"},
		{"missing", "no uuid here", ""},
		{"empty", `"IOPlatformUUID" = ""`, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ParseIOPlatformUUID(tc.input); got != tc.want {
				t.Fatalf("expected %q, got %q", tc.want, got)
			}
		})
	}
}

func TestParseSerial(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name  string
		input string
		want  string
	}{
		{"system", "Serial Number (system): C02XYZ123456\n", "C02XYZ123456"},
		{"plain", "      Serial Number: ABC123\n", "ABC123"},
		{"missing", "Model Name: Mac mini\n", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ParseSerial(tc.input); got != tc.want {
				t.Fatalf("expected %q, got %q", tc.want, got)
			}
		})
	}
}

func TestNormalizeName(t *testing.T) {
	t.Parallel()

	cases := []struct {
		input string
		want  string
	}{
		{"  Mac-Mini_01.local  ", "mac-mini-01.local"},
		{"mac--double", "mac-double"},
		{"UPPER", "upper"},
		{"with/slash", "with-slash"},
		{"", ""},
	}
	for _, tc := range cases {
		if got := normalizeName(tc.input); got != tc.want {
			t.Fatalf("normalize %q: expected %q, got %q", tc.input, tc.want, got)
		}
	}

	long := strings.Repeat("a", 100)
	if got := normalizeName(long); len(got) > maxNameLength {
		t.Fatalf("expected name <= %d chars, got %d", maxNameLength, len(got))
	}
}
