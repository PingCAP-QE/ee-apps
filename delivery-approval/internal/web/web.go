// Package web implements the OIDC-protected request form and detail pages.
// It is deliberately plain net/http + html/template: the surface is HTML, not
// a machine API, and every response is rendered server-side without scripts.
package web

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"embed"
	"encoding/base64"
	"errors"
	"fmt"
	"html/template"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/approval"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/auth"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/config"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/metrics"
	"github.com/PingCAP-QE/ee-apps/delivery-approval/internal/store"
)

const (
	csrfCookie = "approval_csrf"
	maxFiles   = 10
	maxBody    = 64 * 1024
	maxNote    = 1024
)

var (
	tagPattern  = regexp.MustCompile(`^[\w][\w.-]{0,127}$`)
	repoPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]+)?(?:/[A-Za-z0-9._-]+)+$`)
)

var now = func() time.Time { return time.Now().UTC() }

//go:embed templates/*.html
var templateFS embed.FS

type Deliverer interface {
	Deliver(ctx context.Context, requestID string) error
}

type ApprovalNotifier interface {
	NotifyApproval(ctx context.Context, req *store.Request) error
}

type Options struct {
	Store            store.Store
	Config           *config.Manager
	Auth             auth.Validator
	Revoker          approval.Revoker
	Delivery         Deliverer
	ApprovalNotifier ApprovalNotifier
	Metrics          *metrics.Metrics
	Logger           *log.Logger
}

type handler struct {
	store            store.Store
	config           *config.Manager
	auth             auth.Validator
	metrics          *metrics.Metrics
	revoker          approval.Revoker
	delivery         Deliverer
	approvalNotifier ApprovalNotifier
	logger           *log.Logger
	tmpl             *template.Template
	mux              *http.ServeMux
}

func New(opts Options) http.Handler {
	logger := opts.Logger
	if logger == nil {
		logger = log.Default()
	}
	h := &handler{
		store:            opts.Store,
		config:           opts.Config,
		auth:             opts.Auth,
		metrics:          opts.Metrics,
		revoker:          opts.Revoker,
		delivery:         opts.Delivery,
		approvalNotifier: opts.ApprovalNotifier,
		logger:           logger,
		tmpl:             template.Must(template.ParseFS(templateFS, "templates/*.html")),
	}
	h.mux = http.NewServeMux()
	h.mux.HandleFunc("GET /{$}", h.form)
	h.mux.HandleFunc("POST /requests", h.create)
	h.mux.HandleFunc("GET /requests", h.list)
	h.mux.HandleFunc("GET /pending", h.pending)
	h.mux.HandleFunc("GET /requests/{id}", h.detail)
	h.mux.HandleFunc("POST /requests/{id}/approve", h.approve)
	h.mux.HandleFunc("POST /requests/{id}/reject", h.reject)
	h.mux.HandleFunc("POST /requests/{id}/revoke", h.revoke)
	return h
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	secureHeaders(w)
	identity, err := h.authenticate(r)
	if err != nil {
		if errors.Is(err, auth.ErrForbidden) {
			h.deny(w)
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		h.deny(w)
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	r = r.WithContext(auth.WithIdentity(r.Context(), identity))
	h.mux.ServeHTTP(w, r)
}

func (h *handler) authenticate(r *http.Request) (*auth.Identity, error) {
	values := r.Header.Values("X-Approval-Id-Token")
	if len(values) != 1 {
		return nil, auth.ErrUnauthenticated
	}
	token := strings.TrimSpace(values[0])
	if token == "" || len(token) > 8192 {
		return nil, auth.ErrUnauthenticated
	}
	return h.auth.Validate(r.Context(), token)
}

type pageData struct {
	Identity *auth.Identity
	CSRF     string
	Error    string
	Form     formValues

	Request    *store.Request
	Files      []*store.Request
	Pending    []*store.Request
	Approver   bool
	CanApprove bool
	CanRevoke  bool
}

type formValues struct {
	Repository string
	Tag        string
	Files      string
	TTL        string
	Note       string
}

func (h *handler) form(w http.ResponseWriter, r *http.Request) {
	identity, _ := auth.IdentityFrom(r.Context())
	h.render(w, "form.html", pageData{
		Identity: identity,
		CSRF:     csrfToken(w, r),
		Form: formValues{
			TTL: strconv.FormatInt(h.config.Current().DefaultTTLSeconds, 10),
		},
		Approver: h.config.IsApprover(identity.Email),
	})
}

func (h *handler) create(w http.ResponseWriter, r *http.Request) {
	identity, _ := auth.IdentityFrom(r.Context())
	r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	if err := r.ParseForm(); err != nil {
		http.Error(w, "invalid form", http.StatusBadRequest)
		return
	}
	if !validCSRF(r) {
		h.deny(w)
		http.Error(w, "invalid form", http.StatusForbidden)
		return
	}
	values, ttl, err := h.parseForm(r)
	if err != nil {
		h.render(w, "form.html", pageData{Identity: identity, CSRF: csrfToken(w, r), Error: err.Error(), Form: values, Approver: h.config.IsApprover(identity.Email)})
		return
	}

	cfg := h.config.Current()
	ctx := r.Context()
	pending, err := h.store.CountPendingByRequester(ctx, identity.Email)
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	if pending >= cfg.MaxPendingPerRequester {
		h.deny(w)
		http.Error(w, "too many pending requests", http.StatusTooManyRequests)
		return
	}
	recent, err := h.store.CountRequestsSince(ctx, identity.Email, now().Add(-24*time.Hour))
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	if recent >= cfg.MaxRequestsPerDay {
		h.deny(w)
		http.Error(w, "daily request limit reached", http.StatusTooManyRequests)
		return
	}

	req := &store.Request{
		RequesterEmail: identity.Email,
		RequesterSub:   identity.Subject,
		Purpose:        "customer-delivery",
		Note:           values.Note,
		Status:         store.StatusPending,
		TTLSeconds:     ttl,
	}
	for _, filename := range splitFiles(values.Files) {
		req.Files = append(req.Files, store.File{Repository: values.Repository, Tag: values.Tag, Filename: filename})
	}
	if err := h.store.CreateRequest(ctx, req); err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	if h.metrics != nil {
		h.metrics.Created.Add(1)
	}
	if h.approvalNotifier != nil {
		go func(id string) {
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			if err := h.approvalNotifier.NotifyApproval(ctx, req); err != nil {
				h.logger.Printf("approval notification failed request=%s", id)
			}
		}(req.ID)
	}
	http.Redirect(w, r, "/requests/"+req.ID, http.StatusSeeOther)
}

func (h *handler) list(w http.ResponseWriter, r *http.Request) {
	identity, _ := auth.IdentityFrom(r.Context())
	files, err := h.store.ListByRequester(r.Context(), identity.Email, 100)
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	data := pageData{Identity: identity, Files: files, Approver: h.config.IsApprover(identity.Email)}
	if data.Approver {
		pending, err := h.store.ListPending(r.Context(), 100)
		if err != nil {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		data.Pending = pending
	}
	h.render(w, "list.html", data)
}

func (h *handler) detail(w http.ResponseWriter, r *http.Request) {
	identity, _ := auth.IdentityFrom(r.Context())
	req, err := h.store.GetRequest(r.Context(), r.PathValue("id"))
	if errors.Is(err, store.ErrNotFound) {
		http.NotFound(w, r)
		return
	}
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	if req.RequesterEmail != identity.Email && !h.config.IsApprover(identity.Email) {
		http.NotFound(w, r)
		return
	}
	approver := h.config.IsApprover(identity.Email)
	h.render(w, "detail.html", pageData{
		Identity:   identity,
		CSRF:       csrfToken(w, r),
		Request:    req,
		Approver:   approver,
		CanApprove: approver && req.Status == store.StatusPending && req.RequesterEmail != identity.Email,
		CanRevoke: (req.RequesterEmail == identity.Email || approver) &&
			(req.Status == store.StatusPending || req.Status == store.StatusApproved),
	})
}

func (h *handler) pending(w http.ResponseWriter, r *http.Request) {
	identity, _ := auth.IdentityFrom(r.Context())
	if !h.config.IsApprover(identity.Email) {
		h.deny(w)
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	pending, err := h.store.ListPending(r.Context(), 100)
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	h.render(w, "pending.html", pageData{Identity: identity, Pending: pending, Approver: true})
}

func (h *handler) approve(w http.ResponseWriter, r *http.Request) {
	h.decide(w, r, store.StatusApproved)
}

func (h *handler) reject(w http.ResponseWriter, r *http.Request) {
	h.decide(w, r, store.StatusRejected)
}

func (h *handler) decide(w http.ResponseWriter, r *http.Request, to store.Status) {
	identity, _ := auth.IdentityFrom(r.Context())
	r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	if err := r.ParseForm(); err != nil {
		http.Error(w, "invalid form", http.StatusBadRequest)
		return
	}
	if !validCSRF(r) {
		h.deny(w)
		http.Error(w, "invalid form", http.StatusForbidden)
		return
	}
	if !h.config.IsApprover(identity.Email) {
		h.deny(w)
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	req, err := h.store.GetRequest(r.Context(), r.PathValue("id"))
	if errors.Is(err, store.ErrNotFound) {
		http.NotFound(w, r)
		return
	}
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	if req.RequesterEmail == identity.Email {
		h.deny(w)
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	if req.Status != store.StatusPending {
		h.deny(w)
		http.Error(w, "request is not pending", http.StatusConflict)
		return
	}
	note := strings.TrimSpace(r.PostFormValue("note"))
	if utf8.RuneCountInString(note) > maxNote {
		http.Error(w, "note is too long", http.StatusBadRequest)
		return
	}
	ok, err := h.store.Transition(r.Context(), req.ID, store.StatusPending, to, identity.Email, note, now())
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	if !ok {
		h.deny(w)
		http.Error(w, "request was already decided", http.StatusConflict)
		return
	}
	if h.metrics != nil {
		if to == store.StatusApproved {
			h.metrics.Approved.Add(1)
		} else {
			h.metrics.Rejected.Add(1)
		}
	}
	if to == store.StatusApproved && h.delivery != nil {
		go func(id string) {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
			defer cancel()
			if err := h.delivery.Deliver(ctx, id); err != nil {
				h.logger.Printf("delivery failed request=%s", id)
			}
		}(req.ID)
	}
	http.Redirect(w, r, "/requests/"+req.ID, http.StatusSeeOther)
}

func (h *handler) revoke(w http.ResponseWriter, r *http.Request) {
	identity, _ := auth.IdentityFrom(r.Context())
	r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	if err := r.ParseForm(); err != nil {
		http.Error(w, "invalid form", http.StatusBadRequest)
		return
	}
	if !validCSRF(r) {
		h.deny(w)
		http.Error(w, "invalid form", http.StatusForbidden)
		return
	}
	req, err := h.store.GetRequest(r.Context(), r.PathValue("id"))
	if errors.Is(err, store.ErrNotFound) {
		http.NotFound(w, r)
		return
	}
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	approver := h.config.IsApprover(identity.Email)
	if req.RequesterEmail != identity.Email && !approver {
		http.NotFound(w, r)
		return
	}
	if req.Status != store.StatusPending && req.Status != store.StatusApproved {
		h.deny(w)
		http.Error(w, "request cannot be revoked", http.StatusConflict)
		return
	}
	wasApproved := req.Status == store.StatusApproved
	ok, err := h.store.Transition(r.Context(), req.ID, req.Status, store.StatusRevoked, identity.Email, "", now())
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	if !ok {
		h.deny(w)
		http.Error(w, "request was already decided", http.StatusConflict)
		return
	}
	if wasApproved && h.revoker != nil {
		for _, file := range req.Files {
			if file.ShareID == "" {
				continue
			}
			if err := h.revoker.RevokeShare(r.Context(), file.ShareID); err != nil {
				h.logger.Printf("approval share revoke failed request=%s", req.ID)
			}
		}
	}
	http.Redirect(w, r, "/requests/"+req.ID, http.StatusSeeOther)
}

func (h *handler) render(w http.ResponseWriter, name string, data pageData) {
	if err := h.tmpl.ExecuteTemplate(w, name, data); err != nil {
		h.logger.Printf("render %s failed", name)
		http.Error(w, "unavailable", http.StatusInternalServerError)
	}
}

func (h *handler) deny(w http.ResponseWriter) {
	if h.metrics != nil {
		h.metrics.Denied.Add(1)
	}
}

func secureHeaders(w http.ResponseWriter) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
}

func csrfToken(w http.ResponseWriter, r *http.Request) string {
	if c, err := r.Cookie(csrfCookie); err == nil && validToken(c.Value) {
		return c.Value
	}
	var raw [32]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return ""
	}
	token := base64.RawURLEncoding.EncodeToString(raw[:])
	http.SetCookie(w, &http.Cookie{
		Name:     csrfCookie,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		Secure:   true,
		SameSite: http.SameSiteLaxMode,
	})
	return token
}

func validCSRF(r *http.Request) bool {
	c, err := r.Cookie(csrfCookie)
	if err != nil || !validToken(c.Value) {
		return false
	}
	submitted := r.PostFormValue("csrf")
	return subtle.ConstantTimeCompare([]byte(c.Value), []byte(submitted)) == 1
}

func validToken(token string) bool {
	raw, err := base64.RawURLEncoding.Strict().DecodeString(token)
	return err == nil && len(raw) == 32
}

func (h *handler) parseForm(r *http.Request) (formValues, int64, error) {
	cfg := h.config.Current()
	values := formValues{
		Repository: strings.TrimSpace(r.PostFormValue("repository")),
		Tag:        strings.TrimSpace(r.PostFormValue("tag")),
		Files:      strings.TrimSpace(r.PostFormValue("files")),
		TTL:        strings.TrimSpace(r.PostFormValue("ttl")),
		Note:       strings.TrimSpace(r.PostFormValue("note")),
	}
	if values.Repository == "" || values.Tag == "" || values.Files == "" {
		return values, 0, errors.New("repository, tag and files are required")
	}
	if !validRepository(values.Repository) {
		return values, 0, errors.New("invalid repository")
	}
	if !tagPattern.MatchString(values.Tag) {
		return values, 0, errors.New("invalid tag")
	}
	files := splitFiles(values.Files)
	if len(files) == 0 || len(files) > maxFiles {
		return values, 0, fmt.Errorf("provide 1-%d files", maxFiles)
	}
	for _, file := range files {
		if !validFilename(file) {
			return values, 0, fmt.Errorf("invalid filename %q", file)
		}
	}
	if utf8.RuneCountInString(values.Note) > maxNote {
		return values, 0, errors.New("note is too long")
	}
	ttl := cfg.DefaultTTLSeconds
	if values.TTL != "" {
		parsed, err := strconv.ParseInt(values.TTL, 10, 64)
		if err != nil {
			return values, 0, errors.New("invalid TTL")
		}
		ttl = parsed
	}
	if ttl < 1 || ttl > cfg.MaxTTLSeconds {
		return values, 0, fmt.Errorf("TTL must be between 1 and %d seconds", cfg.MaxTTLSeconds)
	}
	return values, ttl, nil
}

func splitFiles(raw string) []string {
	seen := map[string]bool{}
	var out []string
	for _, line := range strings.Split(raw, "\n") {
		name := strings.TrimSpace(line)
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		out = append(out, name)
	}
	return out
}

func validRepository(repo string) bool {
	return len(repo) <= 512 && repoPattern.MatchString(repo) && !strings.Contains(repo, "..")
}

func validFilename(name string) bool {
	if name == "" || len(name) > 255 || !utf8.ValidString(name) || name == "." || name == ".." || strings.ContainsAny(name, "/\\") {
		return false
	}
	return !strings.ContainsFunc(name, unicode.IsControl)
}
