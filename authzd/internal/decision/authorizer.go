package decision

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net/url"
	"strings"

	"github.com/PingCAP-QE/ee-apps/authzd/internal/identity"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/registry"
	"github.com/PingCAP-QE/ee-apps/authzd/internal/resolver"
	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	rpcstatus "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc/codes"
)

const (
	extensionRouteID      = "route_id"
	extensionIdentityMode = "identity_mode"
	extensionUpstreamPath = "upstream_path"
)

type TokenVerifier interface {
	Verify(context.Context, string) (identity.Principal, error)
}

type Credential struct {
	Header   string
	Verifier TokenVerifier
}

type Resolver interface {
	Resolve(context.Context, string, string) (string, error)
	Version() string
}

type Checker interface {
	Check(context.Context, string, string, string, bool) (bool, error)
	ModelID() string
}

// ContextPolicyEvaluator is an optional second-stage ABAC hook. It deliberately
// receives normalized, verified attributes only; raw headers and body data are
// not exposed. No OPA implementation is installed by this service.
type ContextPolicyEvaluator interface {
	Evaluate(context.Context, PolicyContext) (bool, error)
}

type PolicyContext struct {
	Principal  string
	RouteID    string
	Host       string
	Method     string
	Capability string
	Resources  []string
}

type AuditEvent struct {
	DecisionID      string `json:"decision_id"`
	Principal       string `json:"principal,omitempty"`
	Capability      string `json:"capability,omitempty"`
	ResourceType    string `json:"resource_type,omitempty"`
	ResourceID      string `json:"resource_id,omitempty"`
	Outcome         string `json:"outcome"`
	WouldDeny       bool   `json:"would_deny,omitempty"`
	Reason          string `json:"reason_code"`
	ModelID         string `json:"model_id,omitempty"`
	RegistryVersion string `json:"registry_version,omitempty"`
	ResolverVersion string `json:"resolver_version,omitempty"`
	CorrelationID   string `json:"correlation_id,omitempty"`
}

type AuditSink interface {
	Record(context.Context, AuditEvent)
}

type Authorizer struct {
	registry      *registry.Registry
	credentials   map[string]Credential
	resolver      Resolver
	checker       Checker
	audit         AuditSink
	contextPolicy ContextPolicyEvaluator
	// decisionMode bounds how strongly this instance may deny. It exists so an
	// operator can attach external authorization to a live route before the
	// route registry is complete: unmatched routes would otherwise deny every
	// path the registry does not list yet.
	decisionMode string
}

// Decision modes for WithDecisionMode. "shadow" is accepted as a synonym for
// "observe": at this service the two are identical, because Envoy only reaches
// the ext_authz filter after its own authentication has already passed.
const (
	ModeOff     = "off"
	ModeObserve = "observe"
	ModeEnforce = "enforce"
)

// WithDecisionMode bounds the strength of every response. The zero value is
// ModeEnforce, and rule modes still apply inside it.
func (a *Authorizer) WithDecisionMode(mode string) *Authorizer {
	a.decisionMode = mode
	return a
}

// effectiveMode resolves the mode for one decision: a global mode weaker than
// enforce wins, because the operator selected it for the whole instance.
func (a *Authorizer) effectiveMode(ruleMode string) string {
	switch a.decisionMode {
	case ModeOff:
		return ModeOff
	case "observe", "shadow":
		return ModeObserve
	case "", ModeEnforce:
		if ruleMode == "" {
			return ModeEnforce
		}
		return ruleMode
	default:
		return ModeEnforce
	}
}

func (a *Authorizer) WithContextPolicyEvaluator(evaluator ContextPolicyEvaluator) *Authorizer {
	a.contextPolicy = evaluator
	return a
}

func NewAuthorizer(r *registry.Registry, credentials map[string]Credential, resolver Resolver, checker Checker, audit AuditSink) *Authorizer {
	return &Authorizer{registry: r, credentials: credentials, resolver: resolver, checker: checker, audit: audit}
}

func (a *Authorizer) Authorize(ctx context.Context, attrs *authv3.AttributeContext) (*authv3.CheckResponse, error) {
	event := AuditEvent{DecisionID: decisionID()}
	if attrs == nil || attrs.GetRequest() == nil || attrs.GetRequest().GetHttp() == nil || a.registry == nil {
		return a.finish(ctx, event, false, "invalid_request"), nil
	}
	httpRequest := attrs.GetRequest().GetHttp()
	extensions := attrs.GetContextExtensions()
	routeID := strings.TrimSpace(extensions[extensionRouteID])
	identityMode := strings.TrimSpace(extensions[extensionIdentityMode])
	pathValue, queryValue, _ := strings.Cut(httpRequest.GetPath(), "?")
	query, queryErr := url.ParseQuery(queryValue)
	if queryErr != nil {
		return a.finish(ctx, event, false, "invalid_query"), nil
	}
	upstreamPath := strings.TrimSpace(extensions[extensionUpstreamPath])
	event.CorrelationID = safeCorrelationID(httpRequest.GetHeaders()["x-request-id"])
	event.RegistryVersion = a.registry.Version()
	if a.resolver != nil {
		event.ResolverVersion = a.resolver.Version()
	}
	matched, ok := a.registry.Match(registry.Request{
		Host: httpRequest.GetHost(), Route: routeID, Method: httpRequest.GetMethod(),
		ExternalPath: pathValue, UpstreamPath: upstreamPath,
	})
	if !ok {
		return a.finishMode(ctx, event, a.effectiveMode(ModeEnforce), false, "route_not_registered"), nil
	}
	mode := a.effectiveMode(matched.Rule.Mode)
	if mode == ModeOff || matched.Rule.Mode == "off" {
		return a.finishMode(ctx, event, ModeOff, true, "mode_off"), nil
	}
	credential, ok := a.credentials[identityMode]
	if !ok || credential.Verifier == nil || credential.Header == "" {
		return a.finishMode(ctx, event, mode, false, "identity_mode_unconfigured"), nil
	}
	raw, present := headerValue(httpRequest.GetHeaders(), credential.Header)
	if !present {
		return a.finishMode(ctx, event, mode, false, "identity_missing"), nil
	}
	token, tokenOK := parseToken(raw, credential.Header)
	if !tokenOK {
		return a.finishMode(ctx, event, mode, false, "identity_invalid"), nil
	}
	principal, err := credential.Verifier.Verify(ctx, token)
	if err != nil {
		return a.finishMode(ctx, event, mode, false, "identity_invalid"), nil
	}
	event.Principal = safeLogValue(principal.Subject)
	policyContext := PolicyContext{Principal: principal.Subject, RouteID: routeID, Host: httpRequest.GetHost(), Method: httpRequest.GetMethod()}
	for _, check := range matched.Rule.Checks {
		object := check.Object
		if object == "" {
			if a.resolver == nil {
				return a.dependencyFailure(ctx, event, mode, "resolver_unavailable", errors.New("resource resolver unavailable"))
			}
			id, found := resourceID(check.IDFrom, matched.PathValues, query)
			if !found {
				return a.finishMode(ctx, event, mode, false, "resource_id_missing"), nil
			}
			event.ResourceType, event.ResourceID = check.ObjectFrom, safeLogValue(id)
			object, err = a.resolver.Resolve(ctx, check.ObjectFrom, id)
			if err != nil {
				if errors.Is(err, resolver.ErrNotFound) {
					return a.finishMode(ctx, event, mode, false, "resource_not_found"), nil
				}
				return a.dependencyFailure(ctx, event, mode, "resource_resolution_error", fmt.Errorf("resource resolution unavailable: %w", err))
			}
			event.ResourceType, event.ResourceID = splitObject(object)
			policyContext.Resources = append(policyContext.Resources, object)
		} else if strings.HasPrefix(object, "capability:") {
			event.Capability = object
			policyContext.Capability = object
		}
		if a.checker == nil {
			return a.dependencyFailure(ctx, event, mode, "openfga_unavailable", errors.New("OpenFGA checker unavailable"))
		}
		higherConsistency := httpRequest.GetMethod() != "GET" && httpRequest.GetMethod() != "HEAD"
		allowed, err := a.checker.Check(ctx, principal.Subject, object, check.Relation, higherConsistency)
		if err != nil {
			return a.dependencyFailure(ctx, event, mode, "openfga_error", fmt.Errorf("authorization backend unavailable: %w", err))
		}
		if !allowed {
			return a.finishMode(ctx, event, mode, false, "relationship_denied"), nil
		}
	}
	if a.contextPolicy != nil {
		allowed, err := a.contextPolicy.Evaluate(ctx, policyContext)
		if err != nil {
			return a.dependencyFailure(ctx, event, mode, "context_policy_error", fmt.Errorf("context policy evaluation failed: %w", err))
		}
		if !allowed {
			return a.finishMode(ctx, event, mode, false, "context_policy_denied"), nil
		}
	}
	event.ModelID = a.checker.ModelID()
	return a.finishMode(ctx, event, mode, true, "authorized"), nil
}

func (a *Authorizer) finishMode(ctx context.Context, event AuditEvent, mode string, allowed bool, reason string) *authv3.CheckResponse {
	if mode == ModeOff {
		event.Outcome = "allow"
		event.Reason = reason
		if a.audit != nil {
			a.audit.Record(ctx, event)
		}
		return allowedResponse()
	}
	if mode == "observe" || mode == "shadow" {
		if a.checker != nil {
			event.ModelID = a.checker.ModelID()
		}
		event.Outcome = "allow"
		event.WouldDeny = !allowed
		event.Reason = reason
		if a.audit != nil {
			a.audit.Record(ctx, event)
		}
		return allowedResponse()
	}
	return a.finish(ctx, event, allowed, reason)
}

func (a *Authorizer) dependencyFailure(ctx context.Context, event AuditEvent, mode, reason string, err error) (*authv3.CheckResponse, error) {
	if mode != ModeEnforce {
		return a.finishMode(ctx, event, mode, false, reason), nil
	}
	event.Outcome = "error"
	event.Reason = reason
	if a.checker != nil {
		event.ModelID = a.checker.ModelID()
	}
	if a.audit != nil {
		a.audit.Record(ctx, event)
	}
	return nil, err
}

func (a *Authorizer) finish(ctx context.Context, event AuditEvent, allowed bool, reason string) *authv3.CheckResponse {
	event.Reason = reason
	if allowed {
		event.Outcome = "allow"
	} else {
		event.Outcome = "deny"
	}
	if a.checker != nil {
		event.ModelID = a.checker.ModelID()
	}
	if a.audit != nil {
		a.audit.Record(ctx, event)
	}
	if allowed {
		return allowedResponse()
	}
	return deniedResponse()
}

func allowedResponse() *authv3.CheckResponse {
	return &authv3.CheckResponse{
		Status:       &rpcstatus.Status{Code: int32(codes.OK)},
		HttpResponse: &authv3.CheckResponse_OkResponse{OkResponse: &authv3.OkHttpResponse{}},
	}
}

func deniedResponse() *authv3.CheckResponse {
	return &authv3.CheckResponse{
		Status: &rpcstatus.Status{Code: int32(codes.PermissionDenied)},
		HttpResponse: &authv3.CheckResponse_DeniedResponse{DeniedResponse: &authv3.DeniedHttpResponse{
			Status: &typev3.HttpStatus{Code: typev3.StatusCode_Forbidden},
		}},
	}
}

// headerValue returns the raw value of a credential header. Envoy's
// AttributeContext collapses repeated headers into one comma-joined value, so
// the raw value is what parseToken has to judge.
func headerValue(headers map[string]string, name string) (string, bool) {
	value, ok := headers[strings.ToLower(strings.TrimSpace(name))]
	return value, ok
}

// parseToken extracts exactly one credential from a header value. Both the
// Authorization header and any custom forwarded-identity header get the same
// treatment: a quote of two credentials, a whitespace-separated list, or an
// empty value is rejected rather than guessed. A forwarded identity header is
// trusted only because the Gateway wrote it, so an ambiguous value means the
// overwrite did not happen and the header must not authorize anything.
func parseToken(raw, name string) (string, bool) {
	if strings.EqualFold(strings.TrimSpace(name), "authorization") {
		scheme, token, ok := strings.Cut(strings.TrimSpace(raw), " ")
		if !ok || !strings.EqualFold(scheme, "Bearer") || !singleCredential(token) {
			return "", false
		}
		return token, true
	}
	if !singleCredential(raw) {
		return "", false
	}
	return raw, true
}

func singleCredential(value string) bool {
	if value == "" {
		return false
	}
	for _, r := range value {
		if r == ' ' || r == '\t' || r == '\r' || r == '\n' || r == ',' || r == '\x00' {
			return false
		}
	}
	return true
}

func extractToken(headers map[string]string, name string) (string, bool) {
	raw, ok := headerValue(headers, name)
	if !ok {
		return "", false
	}
	return parseToken(raw, name)
}

func resourceID(source string, pathValues map[string]string, query url.Values) (string, bool) {
	prefix, name, found := strings.Cut(source, ".")
	if !found {
		return "", false
	}
	switch prefix {
	case "path":
		value, ok := pathValues[name]
		return value, ok && value != ""
	case "query":
		values := query[name]
		return firstValue(values)
	default:
		return "", false
	}
}

func firstValue(values []string) (string, bool) {
	if len(values) == 0 || values[0] == "" {
		return "", false
	}
	return values[0], true
}

func splitObject(object string) (string, string) {
	typ, id, ok := strings.Cut(object, ":")
	if !ok {
		return "", ""
	}
	return typ, id
}

func safeLogValue(value string) string {
	value = strings.TrimSpace(value)
	if len(value) > 256 || strings.ContainsAny(value, "\r\n\x00") {
		return ""
	}
	return value
}

func safeCorrelationID(value string) string {
	value = strings.TrimSpace(value)
	if len(value) != 36 {
		return ""
	}
	for i, r := range value {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			if r != '-' {
				return ""
			}
			continue
		}
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'f') || (r >= 'A' && r <= 'F')) {
			return ""
		}
	}
	return value
}

func decisionID() string {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return "unavailable"
	}
	return hex.EncodeToString(id[:])
}
