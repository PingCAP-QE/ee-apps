package inventory

import (
	"context"
	"fmt"
	"math"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"

	buildv1alpha1 "github.com/PingCAP-QE/ee-apps/mac-builder-operator/api/v1alpha1"
)

// Runner runs a command and returns its standard output.
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

// Static describes the slowly changing part of a macOS worker.
type Static struct {
	Hostname  string
	IP        string
	Addresses []buildv1alpha1.MacAgentAddress
	OS        buildv1alpha1.MacAgentOS
	Hardware  buildv1alpha1.MacAgentHardware
}

// Provider collects static and runtime inventory from the local machine.
type Provider struct {
	Runner         Runner
	Hostname       func() (string, error)
	InterfaceAddrs func() ([]net.Addr, error)
	Root           string
}

// NewProvider returns a Provider backed by the local macOS commands.
func NewProvider() *Provider {
	return &Provider{
		Runner:         ExecRunner{},
		Hostname:       os.Hostname,
		InterfaceAddrs: net.InterfaceAddrs,
		Root:           "/",
	}
}

// CollectStatic returns hostname, addresses, OS and hardware information. It is
// best-effort: a failed command leaves its field empty instead of failing the
// heartbeat.
func (p *Provider) CollectStatic(ctx context.Context) Static {
	var static Static

	if p.Hostname != nil {
		if hostname, err := p.Hostname(); err == nil {
			static.Hostname = strings.TrimSpace(hostname)
		}
	}
	if p.InterfaceAddrs != nil {
		if addrs, err := p.InterfaceAddrs(); err == nil {
			static.IP = primaryIPv4(addrs)
		}
	}
	if static.IP != "" {
		static.Addresses = append(static.Addresses, buildv1alpha1.MacAgentAddress{
			Type:    buildv1alpha1.MacAgentAddressTypeInternalIP,
			Address: static.IP,
		})
	}
	if static.Hostname != "" {
		static.Addresses = append(static.Addresses, buildv1alpha1.MacAgentAddress{
			Type:    buildv1alpha1.MacAgentAddressTypeHostname,
			Address: static.Hostname,
		})
	}

	if out, err := p.run(ctx, "sw_vers"); err == nil {
		static.OS.Version, static.OS.Build = parseSwVers(out)
	}
	if out, err := p.run(ctx, "uname", "-r"); err == nil {
		if kernel := strings.TrimSpace(out); kernel != "" {
			static.OS.Kernel = "Darwin " + kernel
		}
	}

	static.Hardware.Model = p.sysctlString(ctx, "hw.model")
	static.Hardware.CPUModel = p.sysctlString(ctx, "machdep.cpu.brand_string")
	if cores := p.sysctlInt(ctx, "hw.ncpu"); cores > 0 && cores <= math.MaxInt32 {
		static.Hardware.CPUCores = int32(cores)
	}
	if memory := p.sysctlInt(ctx, "hw.memsize"); memory > 0 {
		static.Hardware.MemoryBytes = memory
	}
	if out, err := p.run(ctx, "df", "-k", p.root()); err == nil {
		if total, _, _, err := parseDF(out); err == nil {
			static.Hardware.DiskTotalBytes = total
		}
	}

	return static
}

// CollectRuntime returns load averages and free disk space. It is best-effort:
// a failed df leaves DiskFreeBytes at -1 so callers can distinguish "unknown"
// from a genuinely full disk (0 bytes free).
func (p *Provider) CollectRuntime(ctx context.Context) buildv1alpha1.MacAgentRuntime {
	runtime := buildv1alpha1.MacAgentRuntime{DiskFreeBytes: -1}

	if out, err := p.run(ctx, "sysctl", "-n", "vm.loadavg"); err == nil {
		if load1, load5, load15, err := parseLoadAvg(out); err == nil {
			runtime.Load1 = load1
			runtime.Load5 = load5
			runtime.Load15 = load15
		}
	}
	if out, err := p.run(ctx, "df", "-k", p.root()); err == nil {
		if _, free, percent, err := parseDF(out); err == nil {
			runtime.DiskFreeBytes = free
			runtime.DiskFreePercent = percent
		}
	}

	return runtime
}

func (p *Provider) run(ctx context.Context, name string, args ...string) (string, error) {
	if p.Runner == nil {
		return "", fmt.Errorf("no command runner configured")
	}
	return p.Runner.Run(ctx, name, args...)
}

func (p *Provider) root() string {
	if strings.TrimSpace(p.Root) == "" {
		return "/"
	}
	return p.Root
}

func (p *Provider) sysctlString(ctx context.Context, key string) string {
	out, err := p.run(ctx, "sysctl", "-n", key)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}

func (p *Provider) sysctlInt(ctx context.Context, key string) int64 {
	out, err := p.run(ctx, "sysctl", "-n", key)
	if err != nil {
		return 0
	}
	value, err := strconv.ParseInt(strings.TrimSpace(out), 10, 64)
	if err != nil {
		return 0
	}
	return value
}

func parseSwVers(output string) (version, build string) {
	for _, line := range strings.Split(output, "\n") {
		switch {
		case strings.HasPrefix(line, "ProductVersion:"):
			version = strings.TrimSpace(strings.TrimPrefix(line, "ProductVersion:"))
		case strings.HasPrefix(line, "BuildVersion:"):
			build = strings.TrimSpace(strings.TrimPrefix(line, "BuildVersion:"))
		}
	}
	return version, build
}

func parseLoadAvg(output string) (load1, load5, load15 float64, err error) {
	output = strings.TrimSpace(output)
	output = strings.TrimPrefix(output, "{")
	output = strings.TrimSuffix(output, "}")
	fields := strings.Fields(output)
	if len(fields) < 3 {
		return 0, 0, 0, fmt.Errorf("malformed load average: %q", output)
	}
	if load1, err = strconv.ParseFloat(fields[0], 64); err != nil {
		return 0, 0, 0, fmt.Errorf("parse load1: %w", err)
	}
	if load5, err = strconv.ParseFloat(fields[1], 64); err != nil {
		return 0, 0, 0, fmt.Errorf("parse load5: %w", err)
	}
	if load15, err = strconv.ParseFloat(fields[2], 64); err != nil {
		return 0, 0, 0, fmt.Errorf("parse load15: %w", err)
	}
	return load1, load5, load15, nil
}

func parseDF(output string) (totalBytes, freeBytes int64, freePercent int32, err error) {
	lines := strings.Split(strings.TrimSpace(output), "\n")
	if len(lines) < 2 {
		return 0, 0, 0, fmt.Errorf("malformed df output: %q", output)
	}
	fields := strings.Fields(lines[len(lines)-1])
	if len(fields) < 5 {
		return 0, 0, 0, fmt.Errorf("malformed df line: %q", lines[len(lines)-1])
	}
	totalKB, err := strconv.ParseInt(fields[1], 10, 64)
	if err != nil || totalKB <= 0 {
		return 0, 0, 0, fmt.Errorf("parse df total: %w", err)
	}
	freeKB, err := strconv.ParseInt(fields[3], 10, 64)
	if err != nil || freeKB < 0 {
		return 0, 0, 0, fmt.Errorf("parse df available: %w", err)
	}

	freePercent = int32(freeKB * 100 / totalKB)
	if capacity := strings.TrimSuffix(fields[4], "%"); capacity != fields[4] {
		if parsed, parseErr := strconv.ParseInt(capacity, 10, 32); parseErr == nil {
			freePercent = int32(parsed)
		}
	}
	if freePercent < 0 {
		freePercent = 0
	}
	if freePercent > 100 {
		freePercent = 100
	}

	return totalKB * 1024, freeKB * 1024, freePercent, nil
}

func primaryIPv4(addrs []net.Addr) string {
	for _, addr := range addrs {
		var ip net.IP
		switch value := addr.(type) {
		case *net.IPNet:
			ip = value.IP
		case *net.IPAddr:
			ip = value.IP
		default:
			continue
		}
		ip4 := ip.To4()
		if ip4 == nil || ip4.IsLoopback() || ip4.IsLinkLocalUnicast() || ip4.IsUnspecified() {
			continue
		}
		return ip4.String()
	}
	return ""
}
