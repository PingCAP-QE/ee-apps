// Package lark sends approval notifications and delivery links over Lark. It
// uses outbound OpenAPI calls only: no WebSocket, no event subscription and no
// card callback handling.
package lark

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strings"
	"sync"

	larksuite "github.com/larksuite/oapi-sdk-go/v3"
	larkcore "github.com/larksuite/oapi-sdk-go/v3/core"
	larkcontact "github.com/larksuite/oapi-sdk-go/v3/service/contact/v3"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/config"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/delivery"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

type Client struct {
	api    *larksuite.Client
	chatID string
	config *config.Manager
	buzz   bool
	logger *log.Logger

	mu      sync.Mutex
	openIDs map[string]string
}

func New(api *larksuite.Client, chatID string, cfg *config.Manager, buzz bool, logger *log.Logger) (*Client, error) {
	if api == nil {
		return nil, errors.New("lark client requires a non-nil API client")
	}
	if cfg == nil {
		return nil, errors.New("lark client requires a non-nil configuration manager")
	}
	if logger == nil {
		logger = log.Default()
	}
	return &Client{api: api, chatID: chatID, config: cfg, buzz: buzz, logger: logger, openIDs: map[string]string{}}, nil
}

// NewDefault builds the production client. The SDK owns tenant-token caching and
// refresh; the service intentionally keeps no token state.
func NewDefault(appID, appSecret, chatID string, cfg *config.Manager, buzz bool, logger *log.Logger) (*Client, error) {
	if appID == "" || appSecret == "" {
		return nil, errors.New("lark app id and secret are required")
	}
	api := larksuite.NewClient(appID, appSecret, larksuite.WithLogLevel(larkcore.LogLevelError))
	return New(api, chatID, cfg, buzz, logger)
}

// NotifyApproval posts a notification-only card and returns the ids of the
// cards it created, so they can be patched once the request is decided.
// Approval itself happens in the browser; no download token is ever included.
func (c *Client) NotifyApproval(ctx context.Context, req *store.Request) ([]store.ApprovalCard, error) {
	card := pendingCard(c.config.Current().PublicURL, req)
	content, err := json.Marshal(card)
	if err != nil {
		return nil, err
	}
	if c.chatID == "" {
		// No shared approval group: deliver the card to each approver's DM.
		targets := c.approverTargets(ctx, c.config.Current().ApproverEmails)
		if len(targets) == 0 {
			return nil, errors.New("no approver open_id")
		}
		cards := make([]store.ApprovalCard, 0, len(targets))
		failed := 0
		for _, target := range targets {
			messageID, err := c.send(ctx, larkim.ReceiveIdTypeOpenId, target.openID, "interactive", string(content))
			if err != nil {
				// Attempt every approver. A partial failure is logged and does
				// not fail the request; only a total failure is an error.
				failed++
				continue
			}
			cards = append(cards, store.ApprovalCard{ApproverEmail: target.email, MessageID: messageID})
		}
		if failed == len(targets) {
			return nil, errors.New("failed to send approval DM to every approver")
		}
		if failed > 0 {
			c.logger.Printf("lark approval DM partial failure request=%s failed=%d/%d", req.ID, failed, len(targets))
		}
		return cards, nil
	}
	messageID, err := c.send(ctx, larkim.ReceiveIdTypeChatId, c.chatID, "interactive", string(content))
	if err != nil {
		return nil, err
	}
	cards := []store.ApprovalCard{{MessageID: messageID}}
	if !c.buzz {
		return cards, nil
	}
	openIDs := c.openIDsFor(ctx, c.config.Current().ApproverEmails)
	if len(openIDs) == 0 {
		return cards, nil
	}
	urgent, err := c.api.Im.Message.UrgentApp(ctx, larkim.NewUrgentAppMessageReqBuilder().
		MessageId(messageID).
		UserIdType(larkim.UserIdTypeUrgentAppMessageOpenId).
		UrgentReceivers(&larkim.UrgentReceivers{UserIdList: openIDs}).
		Build())
	if err != nil || !urgent.Success() {
		// A buzz failure must not change the request state; the card is already
		// delivered and the pending list is the fallback.
		c.logger.Printf("lark urgent app failed request=%s", req.ID)
	}
	return cards, nil
}

// UpdateApprovalCards patches the cards that announced a request to their
// decided state. It is best effort: Lark only patches cards sent in the last
// 14 days, a card may have been recalled, and a failed patch must never change
// the decision. At least one successful patch is enough.
func (c *Client) UpdateApprovalCards(ctx context.Context, req *store.Request, cards []store.ApprovalCard, outcome, actor, note string) error {
	if len(cards) == 0 {
		return nil
	}
	content, err := json.Marshal(decidedCard(c.config.Current().PublicURL, req, outcome, actor, note))
	if err != nil {
		return err
	}
	failed := 0
	for _, card := range cards {
		if card.MessageID == "" {
			continue
		}
		resp, err := c.api.Im.Message.Patch(ctx, larkim.NewPatchMessageReqBuilder().
			MessageId(card.MessageID).
			Body(larkim.NewPatchMessageReqBodyBuilder().Content(string(content)).Build()).
			Build())
		if err != nil || !resp.Success() {
			failed++
			c.logger.Printf("lark card update failed request=%s", req.ID)
		}
	}
	if failed == len(cards) {
		return errors.New("failed to update every approval card")
	}
	return nil
}

// Notify implements delivery.Notifier: a private message with the one-time URLs.
func (c *Client) Notify(ctx context.Context, req *store.Request, links []delivery.Link) error {
	openID, err := c.openID(ctx, req.RequesterEmail)
	if err != nil {
		return err
	}
	var b strings.Builder
	b.WriteString("Your delivery links:\n")
	for _, link := range links {
		fmt.Fprintf(&b, "%s (%s/%s): %s\n", link.File.Filename, link.File.Repository, link.File.Tag, link.URL)
	}
	fmt.Fprintf(&b, "\nRequest %s expires at %s.", req.ID, req.ExpiresAt.UTC().Format("2006-01-02 15:04 UTC"))
	content, err := json.Marshal(map[string]string{"text": b.String()})
	if err != nil {
		return err
	}
	_, err = c.send(ctx, larkim.ReceiveIdTypeOpenId, openID, "text", string(content))
	return err
}

func (c *Client) send(ctx context.Context, receiveIDType, receiveID, msgType, content string) (string, error) {
	resp, err := c.api.Im.Message.Create(ctx, larkim.NewCreateMessageReqBuilder().
		ReceiveIdType(receiveIDType).
		Body(larkim.NewCreateMessageReqBodyBuilder().
			ReceiveId(receiveID).
			MsgType(msgType).
			Content(content).
			Build()).
		Build())
	if err != nil {
		c.logger.Printf("lark send error: %v", err)
		return "", errors.New("lark send failed")
	}
	if !resp.Success() || resp.Data == nil || resp.Data.MessageId == nil {
		c.logger.Printf("lark send rejected: code=%d msg=%s", resp.Code, resp.Msg)
		return "", errors.New("lark rejected the message")
	}
	return *resp.Data.MessageId, nil
}

func (c *Client) openID(ctx context.Context, email string) (string, error) {
	email = strings.ToLower(strings.TrimSpace(email))
	if email == "" {
		return "", errors.New("missing requester email")
	}
	c.mu.Lock()
	if id := c.openIDs[email]; id != "" {
		c.mu.Unlock()
		return id, nil
	}
	c.mu.Unlock()
	ids := c.openIDsFor(ctx, []string{email})
	if len(ids) > 0 && ids[0] != "" {
		return ids[0], nil
	}
	return "", errors.New("lark user not found")
}

func (c *Client) openIDsFor(ctx context.Context, emails []string) []string {
	missing := make([]string, 0, len(emails))
	c.mu.Lock()
	for _, email := range emails {
		email = strings.ToLower(strings.TrimSpace(email))
		if email == "" {
			continue
		}
		if c.openIDs[email] == "" {
			missing = append(missing, email)
		}
	}
	c.mu.Unlock()
	if len(missing) > 0 {
		resp, err := c.api.Contact.User.BatchGetId(ctx, larkcontact.NewBatchGetIdUserReqBuilder().
			UserIdType(larkcontact.UserIdTypeOpenId).
			Body(larkcontact.NewBatchGetIdUserReqBodyBuilder().Emails(missing).Build()).
			Build())
		if err != nil || !resp.Success() || resp.Data == nil {
			c.logger.Printf("lark contact lookup failed")
		} else {
			c.mu.Lock()
			for _, user := range resp.Data.UserList {
				if user == nil || user.Email == nil || user.UserId == nil {
					continue
				}
				c.openIDs[strings.ToLower(*user.Email)] = *user.UserId
			}
			c.mu.Unlock()
		}
	}
	out := make([]string, 0, len(emails))
	c.mu.Lock()
	for _, email := range emails {
		if id := c.openIDs[strings.ToLower(strings.TrimSpace(email))]; id != "" {
			out = append(out, id)
		}
	}
	c.mu.Unlock()
	return out
}

// target pairs an approver email with its open id so a sent card can be
// attributed to the approver who received it.
type target struct {
	email  string
	openID string
}

func (c *Client) approverTargets(ctx context.Context, emails []string) []target {
	c.openIDsFor(ctx, emails)
	out := make([]target, 0, len(emails))
	c.mu.Lock()
	for _, email := range emails {
		key := strings.ToLower(strings.TrimSpace(email))
		if key == "" || c.openIDs[key] == "" {
			continue
		}
		out = append(out, target{email: key, openID: c.openIDs[key]})
	}
	c.mu.Unlock()
	return out
}

// NotifyDecision tells the requester that a request was rejected or revoked.
// Delivery success uses Notify with links; delivery failure uses NotifyFailure.
func (c *Client) NotifyDecision(ctx context.Context, req *store.Request, decision, note string) error {
	openID, err := c.openID(ctx, req.RequesterEmail)
	if err != nil {
		return err
	}
	text := fmt.Sprintf("Request %s was %s.", req.ID, decision)
	if note != "" {
		text += "\nNote: " + note
	}
	content, err := json.Marshal(map[string]string{"text": text})
	if err != nil {
		return err
	}
	_, err = c.send(ctx, larkim.ReceiveIdTypeOpenId, openID, "text", string(content))
	return err
}

// NotifyExpired tells the requester that their request expired. It is a plain
// text DM with no link, so a stale message can never be mistaken for a working
// download.
func (c *Client) NotifyExpired(ctx context.Context, req *store.Request, hadLinks bool) error {
	openID, err := c.openID(ctx, req.RequesterEmail)
	if err != nil {
		return err
	}
	text := fmt.Sprintf("Your delivery request %s expired at %s.\n%s",
		req.ID, req.ExpiresAt.UTC().Format("2006-01-02 15:04 UTC"),
		"Submit a new request if the customer still needs the file.")
	if hadLinks {
		text = fmt.Sprintf("Your delivery request %s expired at %s.\nThe download links issued for it no longer work.\nSubmit a new request if the customer still needs the file.",
			req.ID, req.ExpiresAt.UTC().Format("2006-01-02 15:04 UTC"))
	}
	content, err := json.Marshal(map[string]string{"text": text})
	if err != nil {
		return err
	}
	_, err = c.send(ctx, larkim.ReceiveIdTypeOpenId, openID, "text", string(content))
	return err
}

// NotifyFailure tells the requester that delivery failed. The class is a fixed
// error label, never an upstream response body or token.
func (c *Client) NotifyFailure(ctx context.Context, req *store.Request, class string) error {
	openID, err := c.openID(ctx, req.RequesterEmail)
	if err != nil {
		return err
	}
	text := fmt.Sprintf("Delivery for request %s failed (%s). Please contact the delivery owner.", req.ID, class)
	content, err := json.Marshal(map[string]string{"text": text})
	if err != nil {
		return err
	}
	_, err = c.send(ctx, larkim.ReceiveIdTypeOpenId, openID, "text", string(content))
	return err
}
