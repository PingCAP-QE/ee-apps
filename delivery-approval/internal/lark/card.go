package lark

import (
	"fmt"
	"strings"
	"time"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

// updateMulti marks the card as a shared card so it can be patched after the
// decision. Lark rejects patches for cards without it.
func cardConfig() map[string]any {
	return map[string]any{"wide_screen_mode": true, "update_multi": true}
}

func requestLink(publicURL string, req *store.Request) string {
	return strings.TrimSuffix(publicURL, "/") + "/requests/" + req.ID
}

// pendingCard asks an approver to review the request. It carries a link, never
// a download token.
func pendingCard(publicURL string, req *store.Request) map[string]any {
	return map[string]any{
		"config": cardConfig(),
		"header": map[string]any{
			"template": "blue",
			"title":    map[string]string{"tag": "plain_text", "content": "Delivery request · pending approval"},
		},
		"elements": []any{
			summaryBlock(req),
			map[string]any{
				"tag": "action",
				"actions": []any{map[string]any{
					"tag":  "button",
					"type": "primary",
					"text": map[string]string{"tag": "plain_text", "content": "Review and approve"},
					"url":  requestLink(publicURL, req),
				}},
			},
		},
	}
}

// decidedCard replaces a pending card once the request left the pending state,
// so nobody clicks a stale button and wonders why approval is unavailable.
func decidedCard(publicURL string, req *store.Request, outcome, actor, note string) map[string]any {
	elements := []any{
		summaryBlock(req),
		map[string]any{"tag": "hr"},
		// The decision replaces the request's own fields in place: the header
		// colour, this line and the button label all state the outcome, so the
		// card is never mistaken for one that still waits for a decision.
		map[string]any{
			"tag":  "div",
			"text": map[string]string{"tag": "lark_md", "content": decisionLine(req, outcome, actor, note)},
		},
		map[string]any{
			"tag": "action",
			"actions": []any{map[string]any{
				"tag":  "button",
				"type": "default",
				"text": map[string]string{"tag": "plain_text", "content": decidedButtonLabel(outcome)},
				"url":  requestLink(publicURL, req),
			}},
		},
	}
	return map[string]any{
		"config": cardConfig(),
		"header": map[string]any{
			"template": decidedTemplate(outcome),
			"title":    map[string]string{"tag": "plain_text", "content": decidedTitle(outcome)},
		},
		"elements": elements,
	}
}

// decisionLine is the one line that answers "what happened and who did it".
func decisionLine(req *store.Request, outcome, actor, note string) string {
	line := "**" + decidedState(outcome) + "**"
	when := ""
	if req.DecidedAt != nil && !req.DecidedAt.IsZero() {
		when = req.DecidedAt.UTC().Format("2006-01-02 15:04 UTC")
	}
	switch {
	case actor != "" && when != "":
		line += " by " + actor + " at " + when
	case actor != "":
		line += " by " + actor
	case when != "":
		line += " at " + when
	}
	switch {
	case note != "":
		line += "\nDecision note: " + note
	case outcome == string(store.StatusExpired):
		line += "\nThe download links issued for this request no longer work."
	}
	return line
}

func decidedState(outcome string) string {
	switch outcome {
	case string(store.StatusApproved):
		return "Approved"
	case string(store.StatusRejected):
		return "Rejected"
	case string(store.StatusRevoked):
		return "Revoked"
	case string(store.StatusExpired):
		return "Expired"
	}
	return outcome
}

func decidedButtonLabel(outcome string) string {
	switch outcome {
	case string(store.StatusApproved):
		return "View approved request"
	case string(store.StatusRejected):
		return "View rejected request"
	case string(store.StatusRevoked):
		return "View revoked request"
	case string(store.StatusExpired):
		return "View expired request"
	}
	return "View request"
}

// decidedTemplate colours the header so the outcome is visible without reading:
// green approved, red rejected, orange revoked, grey expired.
func decidedTemplate(outcome string) string {
	switch outcome {
	case string(store.StatusApproved):
		return "green"
	case string(store.StatusRejected):
		return "red"
	case string(store.StatusRevoked):
		return "orange"
	case string(store.StatusExpired):
		return "grey"
	}
	return "blue"
}

func decidedTitle(outcome string) string {
	switch outcome {
	case string(store.StatusApproved):
		return "Delivery request · approved"
	case string(store.StatusRejected):
		return "Delivery request · rejected"
	case string(store.StatusRevoked):
		return "Delivery request · revoked"
	case string(store.StatusExpired):
		return "Delivery request · expired"
	}
	return "Delivery request · " + outcome
}

func summaryBlock(req *store.Request) map[string]any {
	return map[string]any{
		"tag": "div",
		"text": map[string]string{
			"tag": "plain_text",
			"content": fmt.Sprintf("Requester: %s\nTargets:\n%s\nTTL: %s\nRequest note: %s",
				req.RequesterEmail, targetLines(req),
				(time.Duration(req.TTLSeconds) * time.Second).String(), req.Note),
		},
	}
}

// targetLines lists every requested file as "<repository> <tag> <filename>", one
// per line. A request may span several repositories, so the card cannot show a
// single repository/tag plus a filename list: the approver has to see exactly
// which files will be issued.
func targetLines(req *store.Request) string {
	if len(req.Files) == 0 {
		return "(none)"
	}
	lines := make([]string, 0, len(req.Files))
	for _, f := range req.Files {
		lines = append(lines, f.Repository+" "+f.Tag+" "+f.Filename)
	}
	return strings.Join(lines, "\n")
}
