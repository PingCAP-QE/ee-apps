package impl

import (
	"errors"
	"fmt"

	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/config"
	"github.com/PingCAP-QE/ee-apps/tibuild/pkg/delivery"
)

// ErrPublishRulesUnavailable means the delivery rules are not readable yet, so
// the target catalogue cannot be checked. The deployment mounts the file with
// Flux, which may land after the process starts.
var ErrPublishRulesUnavailable = errors.New("delivery rules are not available")

// ValidatePublishTargets checks that every target the publish worker claims
// exists in the delivery rules, and fails fast otherwise: a typo would leave
// those steps pending forever, which looks like a stuck queue instead of a
// configuration error.
func ValidatePublishTargets(cfg *config.Service) error {
	workerCfg := cfg.Worker
	workerCfg.ApplyDefaults()
	// Only the image kind claims registry hosts from the delivery rules. A TiUP
	// worker claims tiup-staging/tiup-prod, which are not rule destinations.
	if !containsString(workerCfg.Kinds, PublishKindImageCopy) {
		return nil
	}
	targets := cfg.Publish.PublicationTargets(workerCfg.Targets)
	if len(targets) == 0 || hasWildcardTarget(targets) {
		return nil
	}

	rules, err := delivery.LoadFile(cfg.Publish.DeliveryConfigPathOrDefault())
	if err != nil {
		return fmt.Errorf("%w: %w", ErrPublishRulesUnavailable, err)
	}

	known := make(map[string]struct{})
	for _, target := range rules.Targets() {
		known[target] = struct{}{}
	}
	for _, target := range targets {
		if _, ok := known[target]; !ok {
			return fmt.Errorf("publish.targets contains %q, which is not a destination in the delivery rules (known: %v)", target, rules.Targets())
		}
	}
	return nil
}
