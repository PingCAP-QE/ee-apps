package controller

import (
	"fmt"
	"strings"
	"time"
)

type macAgentSummaryInput struct {
	Phase            string
	OnlineDuration   string
	IP               string
	Arch             string
	CPUCores         int32
	MemoryBytes      int64
	Load1            float64
	Load5            float64
	Load15           float64
	DiskFreeBytes    int64
	ActiveBuilds     int32
	Capacity         int32
	Builds           []string
	LastHeartbeatAge string
}

func formatMacAgentSummary(in macAgentSummaryInput) string {
	phase := strings.TrimSpace(in.Phase)
	if phase == "" {
		phase = "Unknown"
	}
	if in.OnlineDuration != "" {
		phase += " " + in.OnlineDuration
	}
	parts := []string{phase}

	if in.IP != "" {
		parts = append(parts, in.IP)
	}
	if in.Arch != "" {
		parts = append(parts, in.Arch)
	}
	if in.CPUCores > 0 || in.MemoryBytes > 0 {
		parts = append(parts, fmt.Sprintf("%dC/%s", in.CPUCores, humanBytes(in.MemoryBytes)))
	}
	if in.Load1 != 0 || in.Load5 != 0 || in.Load15 != 0 {
		parts = append(parts, fmt.Sprintf("load %.2f/%.2f/%.2f", in.Load1, in.Load5, in.Load15))
	}
	if in.DiskFreeBytes > 0 {
		parts = append(parts, fmt.Sprintf("disk %s free", humanDisk(in.DiskFreeBytes)))
	}
	if len(in.Builds) > 0 {
		parts = append(parts, fmt.Sprintf("build %s (%d/%d)", strings.Join(in.Builds, ", "), in.ActiveBuilds, in.Capacity))
	} else {
		parts = append(parts, "no active builds")
	}

	summary := strings.Join(parts, " | ")
	if age := strings.TrimSpace(in.LastHeartbeatAge); age != "" {
		summary += fmt.Sprintf(" (last heartbeat %s ago)", age)
	}
	return summary
}

func humanDuration(duration time.Duration) string {
	if duration <= 0 {
		return "0s"
	}
	if duration < time.Minute {
		return fmt.Sprintf("%ds", int(duration.Seconds()))
	}
	if duration < time.Hour {
		return fmt.Sprintf("%dm", int(duration.Minutes()))
	}
	if duration < 24*time.Hour {
		hours := int(duration.Hours())
		minutes := int(duration.Minutes()) % 60
		if minutes == 0 {
			return fmt.Sprintf("%dh", hours)
		}
		return fmt.Sprintf("%dh%dm", hours, minutes)
	}
	days := int(duration.Hours()) / 24
	hours := int(duration.Hours()) % 24
	if hours == 0 {
		return fmt.Sprintf("%dd", days)
	}
	return fmt.Sprintf("%dd%dh", days, hours)
}

func humanBytes(bytes int64) string {
	if bytes <= 0 {
		return "0B"
	}
	const unit = 1024
	units := []string{"B", "KiB", "MiB", "GiB", "TiB", "PiB"}
	value := float64(bytes)
	unitIndex := 0
	for value >= unit && unitIndex < len(units)-1 {
		value /= unit
		unitIndex++
	}
	if value == float64(int64(value)) {
		return fmt.Sprintf("%.0f%s", value, units[unitIndex])
	}
	if value >= 10 {
		return fmt.Sprintf("%.0f%s", value, units[unitIndex])
	}
	return fmt.Sprintf("%.1f%s", value, units[unitIndex])
}

// humanDisk renders disk sizes in the compact "Gi" form used in summaries.
func humanDisk(bytes int64) string {
	value := humanBytes(bytes)
	if strings.HasSuffix(value, "iB") {
		return strings.TrimSuffix(value, "iB") + "i"
	}
	return value
}
