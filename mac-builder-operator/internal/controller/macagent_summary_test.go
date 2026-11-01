package controller

import (
	"bytes"
	"testing"
	"time"
)

func TestHumanDuration(t *testing.T) {
	t.Parallel()

	cases := []struct {
		duration time.Duration
		want     string
	}{
		{0, "0s"},
		{-time.Second, "0s"},
		{45 * time.Second, "45s"},
		{12 * time.Minute, "12m"},
		{time.Hour, "1h"},
		{3*time.Hour + 12*time.Minute, "3h12m"},
		{26 * time.Hour, "1d2h"},
		{48 * time.Hour, "2d"},
	}
	for _, tc := range cases {
		if got := humanDuration(tc.duration); got != tc.want {
			t.Fatalf("humanDuration(%s): expected %q, got %q", tc.duration, tc.want, got)
		}
	}
}

func TestHumanBytes(t *testing.T) {
	t.Parallel()

	cases := []struct {
		bytes int64
		want  string
	}{
		{0, "0B"},
		{512, "512B"},
		{1024, "1KiB"},
		{1536, "1.5KiB"},
		{16 * 1024 * 1024 * 1024, "16GiB"},
		{232 * 1024 * 1024 * 1024, "232GiB"},
		{10 * 1024, "10KiB"},
	}
	for _, tc := range cases {
		if got := humanBytes(tc.bytes); got != tc.want {
			t.Fatalf("humanBytes(%d): expected %q, got %q", tc.bytes, tc.want, got)
		}
	}
}

func TestFormatMacAgentSummary(t *testing.T) {
	t.Parallel()

	online := macAgentSummaryInput{
		Phase:          "Online",
		OnlineDuration: "3h12m",
		IP:             "10.20.30.40",
		Arch:           "arm64",
		CPUCores:       8,
		MemoryBytes:    16 * 1024 * 1024 * 1024,
		Load1:          1.25,
		Load5:          1.10,
		Load15:         0.95,
		DiskFreeBytes:  232 * 1024 * 1024 * 1024,
		ActiveBuilds:   1,
		Capacity:       1,
		Builds:         []string{"ee-cd/macbuild-abc"},
	}
	want := "Online 3h12m | 10.20.30.40 | arm64 | 8C/16GiB | load 1.25/1.10/0.95 | disk 232Gi free | build ee-cd/macbuild-abc (1/1)"
	if got := formatMacAgentSummary(online); got != want {
		t.Fatalf("unexpected summary:\nwant: %s\n got: %s", want, got)
	}

	offline := macAgentSummaryInput{
		Phase:            "Offline",
		IP:               "10.20.30.40",
		Arch:             "amd64",
		CPUCores:         4,
		MemoryBytes:      8 * 1024 * 1024 * 1024,
		DiskFreeBytes:    100 * 1024 * 1024 * 1024,
		LastHeartbeatAge: "5m",
	}
	got := formatMacAgentSummary(offline)
	want = "Offline | 10.20.30.40 | amd64 | 4C/8GiB | disk 100Gi free | no active builds (last heartbeat 5m ago)"
	if got != want {
		t.Fatalf("unexpected offline summary:\nwant: %s\n got: %s", want, got)
	}

	multi := macAgentSummaryInput{
		Phase:        "Online",
		ActiveBuilds: 2,
		Capacity:     4,
		Builds:       []string{"ee-cd/a", "ee-cd/b"},
	}
	if got := formatMacAgentSummary(multi); !bytes.Contains([]byte(got), []byte("build ee-cd/a, ee-cd/b (2/4)")) {
		t.Fatalf("unexpected multi-build summary: %s", got)
	}
}
