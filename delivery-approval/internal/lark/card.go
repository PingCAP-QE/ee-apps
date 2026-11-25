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
			"title": map[string]string{"tag": "plain_text", "content": "Delivery request pending approval"},
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
	elements := []any{summaryBlock(req)}
	line := "Status: " + outcome
	if actor != "" {
		line += " by " + actor
	}
	if req.DecidedAt != nil && !req.DecidedAt.IsZero() {
		line += " at " + req.DecidedAt.UTC().Format("2006-01-02 15:04 UTC")
	}
	if note != "" {
		line += "\nNote: " + note
	}
	elements = append(elements, map[string]any{
		"tag":  "div",
		"text": map[string]string{"tag": "plain_text", "content": line},
	})
	elements = append(elements, map[string]any{
		"tag": "action",
		"actions": []any{map[string]any{
			"tag":  "button",
			"type": "default",
			"text": map[string]string{"tag": "plain_text", "content": "Open request"},
			"url":  requestLink(publicURL, req),
		}},
	})
	return map[string]any{
		"config": cardConfig(),
		"header": map[string]any{
			"title": map[string]string{"tag": "plain_text", "content": decidedTitle(outcome)},
		},
		"elements": elements,
	}
}

func decidedTitle(outcome string) string {
	switch outcome {
	case string(store.StatusApproved):
		return "Delivery request approved"
	case string(store.StatusRejected):
		return "Delivery request rejected"
	case string(store.StatusRevoked):
		return "Delivery request revoked"
	case string(store.StatusExpired):
		return "Delivery request expired"
	}
	return "Delivery request " + outcome
}

func summaryBlock(req *store.Request) map[string]any {
	return map[string]any{
		"tag": "div",
		"text": map[string]string{
			"tag": "plain_text",
			"content": fmt.Sprintf("Requester: %s\nRepository: %s\nTag: %s\nFiles: %s\nTTL: %s\nNote: %s",
				req.RequesterEmail, firstFile(req).Repository, firstFile(req).Tag,
				fileNames(req), (time.Duration(req.TTLSeconds) * time.Second).String(), req.Note),
		},
	}
}
