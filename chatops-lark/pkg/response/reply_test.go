package response

import (
	"strings"
	"testing"
)

func TestNewLarkCardWithGoTemplatePreservesQuotesInMarkdown(t *testing.T) {
	card, err := newLarkCardWithGoTemplate(&info{
		Status:  "success",
		Message: "version: \"9\"",
	})
	if err != nil {
		t.Fatalf("newLarkCardWithGoTemplate() error = %v", err)
	}
	if strings.Contains(card, "&#34;") {
		t.Fatalf("expected rendered card to preserve quotes, got: %s", card)
	}
	if !strings.Contains(card, "\\\"9\\\"") {
		t.Fatalf("expected rendered card JSON to contain quoted value, got: %s", card)
	}
}
