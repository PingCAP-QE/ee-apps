package inventory

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
)

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

func staticOutputs() map[string]string {
	return map[string]string{
		"sw_vers":                            "ProductName:\t\tmacOS\nProductVersion:\t\t14.6.1\nBuildVersion:\t\t23G93\n",
		"uname -r":                           "23.6.0\n",
		"sysctl -n hw.model":                 "Mac14,2\n",
		"sysctl -n machdep.cpu.brand_string": "Apple M2\n",
		"sysctl -n hw.ncpu":                  "8\n",
		"sysctl -n hw.memsize":               "17179869184\n",
		"df -k /":                            "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk1 494384795 100 249108103 50% /\n",
	}
}

func testAddrs() []net.Addr {
	return []net.Addr{
		&net.IPNet{IP: net.ParseIP("127.0.0.1"), Mask: net.CIDRMask(8, 32)},
		&net.IPNet{IP: net.ParseIP("::1"), Mask: net.CIDRMask(128, 128)},
		&net.IPNet{IP: net.ParseIP("169.254.1.1"), Mask: net.CIDRMask(16, 32)},
		&net.IPNet{IP: net.ParseIP("10.0.0.5"), Mask: net.CIDRMask(24, 32)},
	}
}

func TestCollectStatic(t *testing.T) {
	t.Parallel()

	provider := Provider{
		Runner:         fakeRunner{outputs: staticOutputs()},
		Hostname:       func() (string, error) { return "mac-mini-01", nil },
		InterfaceAddrs: func() ([]net.Addr, error) { return testAddrs(), nil },
	}
	got := provider.CollectStatic(context.Background())

	if got.Hostname != "mac-mini-01" {
		t.Fatalf("unexpected hostname: %q", got.Hostname)
	}
	if got.IP != "10.0.0.5" {
		t.Fatalf("unexpected primary IP: %q", got.IP)
	}
	if len(got.Addresses) != 2 || got.Addresses[0].Type != "InternalIP" || got.Addresses[1].Type != "Hostname" {
		t.Fatalf("unexpected addresses: %#v", got.Addresses)
	}
	if got.OS.Version != "14.6.1" || got.OS.Build != "23G93" || got.OS.Kernel != "Darwin 23.6.0" {
		t.Fatalf("unexpected OS: %#v", got.OS)
	}
	if got.Hardware.Model != "Mac14,2" || got.Hardware.CPUModel != "Apple M2" || got.Hardware.CPUCores != 8 {
		t.Fatalf("unexpected hardware: %#v", got.Hardware)
	}
	if got.Hardware.MemoryBytes != 17179869184 {
		t.Fatalf("unexpected memory: %d", got.Hardware.MemoryBytes)
	}
	if got.Hardware.DiskTotalBytes != 494384795*1024 {
		t.Fatalf("unexpected disk total: %d", got.Hardware.DiskTotalBytes)
	}
}

func TestCollectRuntime(t *testing.T) {
	t.Parallel()

	provider := Provider{
		Runner: fakeRunner{outputs: map[string]string{
			"sysctl -n vm.loadavg": "{ 1.25 1.10 0.95 }\n",
			"df -k /":              "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk1 494384795 100 249108103 50% /\n",
		}},
	}
	got := provider.CollectRuntime(context.Background())

	if got.Load1 != 1.25 || got.Load5 != 1.10 || got.Load15 != 0.95 {
		t.Fatalf("unexpected load: %#v", got)
	}
	if got.DiskFreeBytes != 249108103*1024 {
		t.Fatalf("unexpected disk free: %d", got.DiskFreeBytes)
	}
	if got.DiskFreePercent != 50 {
		t.Fatalf("unexpected disk percent: %d", got.DiskFreePercent)
	}
}

func TestCollectStaticBestEffort(t *testing.T) {
	t.Parallel()

	provider := Provider{
		Runner:         fakeRunner{},
		Hostname:       func() (string, error) { return "", errors.New("hostname unavailable") },
		InterfaceAddrs: func() ([]net.Addr, error) { return nil, errors.New("no interfaces") },
	}
	got := provider.CollectStatic(context.Background())
	if got.Hostname != "" || got.IP != "" || len(got.Addresses) != 0 {
		t.Fatalf("expected empty identity fields, got %#v", got)
	}
	if got.OS.Version != "" || got.Hardware.CPUCores != 0 {
		t.Fatalf("expected empty OS/hardware, got %#v %#v", got.OS, got.Hardware)
	}

	runtime := provider.CollectRuntime(context.Background())
	if runtime.Load1 != 0 || runtime.DiskFreeBytes != -1 {
		t.Fatalf("expected unknown runtime values, got %#v", runtime)
	}
}

func TestPrimaryIPv4SkipsSpecialAddresses(t *testing.T) {
	t.Parallel()

	if got := primaryIPv4(testAddrs()); got != "10.0.0.5" {
		t.Fatalf("unexpected primary IPv4: %q", got)
	}
	if got := primaryIPv4(nil); got != "" {
		t.Fatalf("expected empty primary IPv4, got %q", got)
	}
}

func TestParseLoadAvg(t *testing.T) {
	t.Parallel()

	l1, l5, l15, err := parseLoadAvg("{ 0.10 1.20 3.30 }")
	if err != nil {
		t.Fatalf("parse loadavg: %v", err)
	}
	if l1 != 0.10 || l5 != 1.20 || l15 != 3.30 {
		t.Fatalf("unexpected load values: %f %f %f", l1, l5, l15)
	}
	if _, _, _, err := parseLoadAvg("not a load average"); err == nil {
		t.Fatal("expected an error for malformed loadavg")
	}
}

func TestParseDF(t *testing.T) {
	t.Parallel()

	total, free, percent, err := parseDF("Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk1 1000 100 250 25% /\n")
	if err != nil {
		t.Fatalf("parse df: %v", err)
	}
	if total != 1000*1024 || free != 250*1024 || percent != 25 {
		t.Fatalf("unexpected df values: total=%d free=%d percent=%d", total, free, percent)
	}
	if _, _, _, err := parseDF("Filesystem\n"); err == nil {
		t.Fatal("expected an error for malformed df output")
	}
}
