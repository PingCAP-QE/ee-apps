// Package dl is a small client for dl's capability API. It never logs the
// issuer secret or the returned download URL.
package dl

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

var (
	ErrInvalidRequest = errors.New("dl rejected the request")
	ErrUnauthorized   = errors.New("dl rejected the issuer credentials")
	ErrNotFound       = errors.New("dl share not found")
	ErrUnavailable    = errors.New("dl unavailable")
)

type IssueRequest struct {
	Repository       string `json:"repository"`
	Tag              string `json:"tag"`
	File             string `json:"file"`
	ExpiresInSeconds int64  `json:"expires_in_seconds"`
	Purpose          string `json:"purpose"`
	CustomerID       string `json:"customer_id,omitempty"`
}

type Share struct {
	ID          string `json:"id"`
	DownloadURL string `json:"download_url"`
}

type Client struct {
	baseURL string
	secret  string
	http    *http.Client
	retries int
	backoff time.Duration
	sleep   func(time.Duration)
}

func New(baseURL, secret string, httpClient *http.Client) (*Client, error) {
	u, err := url.Parse(baseURL)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return nil, errors.New("invalid dl base URL")
	}
	if strings.TrimSpace(secret) == "" {
		return nil, errors.New("dl issuer secret is required")
	}
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}
	return &Client{
		baseURL: strings.TrimSuffix(baseURL, "/"),
		secret:  secret,
		http:    httpClient,
		retries: 2,
		backoff: 200 * time.Millisecond,
		sleep:   time.Sleep,
	}, nil
}

// Issue creates one share. The returned URL is the only copy of the plaintext
// capability; callers must seal it immediately.
func (c *Client) Issue(ctx context.Context, req IssueRequest) (*Share, error) {
	body, err := json.Marshal(map[string]any{
		"artifact": map[string]string{
			"repository": req.Repository,
			"tag":        req.Tag,
			"file":       req.File,
		},
		"expires_in_seconds": req.ExpiresInSeconds,
		"purpose":            req.Purpose,
		"customer_id":        req.CustomerID,
	})
	if err != nil {
		return nil, err
	}
	var share Share
	err = c.do(ctx, http.MethodPost, "/api/v1/shares", body, func(resp *http.Response) error {
		if resp.StatusCode != http.StatusCreated {
			return statusError(resp.StatusCode)
		}
		if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&share); err != nil {
			return ErrUnavailable
		}
		if share.ID == "" || share.DownloadURL == "" {
			return ErrUnavailable
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &share, nil
}

// Revoke withdraws a share. 404 is treated as success because the caller's goal
// is "this share must no longer work".
func (c *Client) Revoke(ctx context.Context, shareID string) error {
	if shareID == "" {
		return nil
	}
	return c.do(ctx, http.MethodDelete, "/api/v1/shares/"+url.PathEscape(shareID), nil, func(resp *http.Response) error {
		switch resp.StatusCode {
		case http.StatusNoContent, http.StatusNotFound:
			return nil
		case http.StatusUnauthorized, http.StatusForbidden:
			return ErrUnauthorized
		default:
			return statusError(resp.StatusCode)
		}
	})
}

// RevokeShare satisfies the approval sweeper's revoker interface.
func (c *Client) RevokeShare(ctx context.Context, shareID string) error {
	return c.Revoke(ctx, shareID)
}

func (c *Client) do(ctx context.Context, method, path string, body []byte, handle func(*http.Response) error) error {
	var last error
	for attempt := 0; attempt <= c.retries; attempt++ {
		if attempt > 0 {
			c.sleep(c.backoff * time.Duration(attempt))
		}
		var reader io.Reader
		if body != nil {
			reader = bytes.NewReader(body)
		}
		req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
		if err != nil {
			return ErrInvalidRequest
		}
		req.Header.Set("Authorization", "Bearer "+c.secret)
		req.Header.Set("Accept", "application/json")
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := c.http.Do(req)
		if err != nil {
			last = ErrUnavailable
			continue
		}
		handleErr := handle(resp)
		_ = resp.Body.Close()
		if handleErr == nil {
			return nil
		}
		if !retryable(handleErr) {
			return handleErr
		}
		last = handleErr
	}
	if last == nil {
		return ErrUnavailable
	}
	return last
}

func statusError(code int) error {
	switch {
	case code == http.StatusBadRequest:
		return ErrInvalidRequest
	case code == http.StatusUnauthorized || code == http.StatusForbidden:
		return ErrUnauthorized
	case code == http.StatusNotFound:
		return ErrNotFound
	case code == http.StatusTooManyRequests || code >= 500:
		return ErrUnavailable
	default:
		return fmt.Errorf("%w: status %d", ErrInvalidRequest, code)
	}
}

func retryable(err error) bool {
	return errors.Is(err, ErrUnavailable)
}
