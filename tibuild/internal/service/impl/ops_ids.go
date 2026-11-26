package impl

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/PingCAP-QE/ee-apps/tibuild/internal/service/gen/hotfix"
)

// opsIDPattern matches the canonical UUID form the ops platform issues for step
// keys and execution ids. Both fields are optional and independent: the service
// only checks that a value which is present is well formed, because a malformed
// id would be frozen into the tag annotation and forwarded to the ops build
// callback as-is.
var opsIDPattern = regexp.MustCompile(
	`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`,
)

// validateOptionalOpsID validates one optional ops id.
func validateOptionalOpsID(field string, value *string) error {
	if value == nil || *value == "" {
		return nil
	}
	if !opsIDPattern.MatchString(*value) {
		return fmt.Errorf("%s %q is not a UUID", field, *value)
	}
	return nil
}

// validateBumpTagOpsIDs validates the optional ops ids of a bump request.
func validateBumpTagOpsIDs(meta *hotfix.TiDBxBumpTagMeta) error {
	if meta == nil || meta.OpsReq == nil {
		return nil
	}
	if err := validateOptionalOpsID("step_key", meta.OpsReq.StepKey); err != nil {
		return err
	}
	return validateOptionalOpsID("execution_id", meta.OpsReq.ExecutionID)
}

// normalizeBumpTagOpsIDs clears the ops ids that carry no value, so they stay
// out of the tag annotation. The struct tags use `omitempty`, but that only
// omits a nil pointer: a caller that sends `"step_key": ""` would otherwise
// freeze an empty string into the annotation that every later reader parses.
//
// It runs before validation, so whitespace-only input is dropped rather than
// reported as a malformed UUID. The caller's own response echoes the normalized
// meta, which is what the tag now records.
func normalizeBumpTagOpsIDs(meta *hotfix.TiDBxBumpTagMeta) {
	if meta == nil || meta.OpsReq == nil {
		return
	}
	for _, field := range []**string{
		&meta.OpsReq.Applicant,
		&meta.OpsReq.ReleaseID,
		&meta.OpsReq.ChangeID,
		&meta.OpsReq.StepKey,
		&meta.OpsReq.ExecutionID,
	} {
		if *field != nil && strings.TrimSpace(**field) == "" {
			*field = nil
		}
	}
}
