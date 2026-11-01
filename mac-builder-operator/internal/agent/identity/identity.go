package identity

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"strings"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

const maxNameLength = 63

// Runner runs a command and returns its combined output. It is injectable so
// identity and inventory collection can be tested without macOS commands.
type Runner interface {
	Run(ctx context.Context, name string, args ...string) (string, error)
}

// ExecRunner runs commands on the local machine.
type ExecRunner struct{}

// Run executes the command and returns its standard output.
func (ExecRunner) Run(ctx context.Context, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// Machine is the stable identity derived for a macOS worker.
type Machine struct {
	// Name is the normalized MacAgent object name.
	Name string
	// Source is the identifier source: platform-uuid, serial, or hostname.
	Source string
	// Value is the raw identifier value.
	Value string
}

// Resolver derives a stable machine identity from the platform UUID, serial
// number, or hostname. The macOS hardware UUID is preferred because it survives
// hostname changes, reinstallation and OS upgrades.
type Resolver struct {
	Runner   Runner
	Override string
	Hostname func() (string, error)
}

// Resolve returns the machine identity or an error when no identifier source is
// available.
func (r Resolver) Resolve(ctx context.Context) (Machine, error) {
	hostname := ""
	var hostnameErr error
	if r.Hostname != nil {
		hostname, hostnameErr = r.Hostname()
	}

	if r.Runner != nil {
		if out, err := r.Runner.Run(ctx, "ioreg", "-rd1", "-c", "IOPlatformExpertDevice"); err == nil {
			if uuid := ParseIOPlatformUUID(out); uuid != "" {
				return r.withOverride(Machine{
					Name:   NameFromUUID(uuid),
					Source: buildv1alpha1.MacAgentMachineIDSourcePlatformUUID,
					Value:  uuid,
				}), nil
			}
		}
		if out, err := r.Runner.Run(ctx, "system_profiler", "SPHardwareDataType"); err == nil {
			if serial := ParseSerial(out); serial != "" {
				return r.withOverride(Machine{
					Name:   NameFromSerial(serial),
					Source: buildv1alpha1.MacAgentMachineIDSourceSerial,
					Value:  serial,
				}), nil
			}
		}
	}

	if hostnameErr != nil || strings.TrimSpace(hostname) == "" {
		if hostnameErr == nil {
			hostnameErr = fmt.Errorf("hostname is empty")
		}
		return Machine{}, fmt.Errorf("cannot determine machine identity: %w", hostnameErr)
	}
	return r.withOverride(Machine{
		Name:   NameFromHostname(hostname),
		Source: buildv1alpha1.MacAgentMachineIDSourceHostname,
		Value:  hostname,
	}), nil
}

func (r Resolver) withOverride(machine Machine) Machine {
	if normalized := normalizeName(r.Override); normalized != "" {
		machine.Name = normalized
	}
	return machine
}

// NameFromUUID derives the MacAgent name from an IOPlatformUUID.
func NameFromUUID(uuid string) string {
	return normalizeName("mac-" + strings.ToLower(strings.TrimSpace(uuid)))
}

// NameFromSerial derives the MacAgent name from a hardware serial number.
func NameFromSerial(serial string) string {
	return normalizeName("mac-serial-" + strings.ToLower(strings.TrimSpace(serial)))
}

// NameFromHostname derives a stable MacAgent name from a hostname.
func NameFromHostname(hostname string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(hostname)))
	return "mac-" + hex.EncodeToString(sum[:])[:12]
}

// ParseIOPlatformUUID extracts IOPlatformUUID from `ioreg` output.
func ParseIOPlatformUUID(output string) string {
	for _, line := range strings.Split(output, "\n") {
		if !strings.Contains(line, "IOPlatformUUID") {
			continue
		}
		_, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		value = strings.TrimSpace(value)
		value = strings.Trim(value, `"`)
		if value != "" {
			return value
		}
	}
	return ""
}

// ParseSerial extracts the hardware serial number from `system_profiler` output.
func ParseSerial(output string) string {
	for _, line := range strings.Split(output, "\n") {
		if !strings.Contains(line, "Serial Number") {
			continue
		}
		idx := strings.LastIndex(line, ":")
		if idx < 0 {
			continue
		}
		value := strings.TrimSpace(line[idx+1:])
		if value != "" {
			return value
		}
	}
	return ""
}

func normalizeName(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	var b strings.Builder
	lastDash := false
	for _, r := range value {
		valid := (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '.' || r == '-'
		if !valid {
			if b.Len() > 0 && !lastDash {
				b.WriteByte('-')
				lastDash = true
			}
			continue
		}
		if r == '-' {
			if b.Len() == 0 || lastDash {
				continue
			}
			lastDash = true
		} else {
			lastDash = false
		}
		b.WriteRune(r)
	}
	out := strings.Trim(b.String(), "-.")
	if len(out) > maxNameLength {
		out = strings.TrimRight(out[:maxNameLength], "-.")
	}
	return out
}

// Hostname returns the local hostname. It is a package variable so tests and
// embedders can replace it.
var Hostname = os.Hostname
