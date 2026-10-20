package authz

import (
	"errors"
	"fmt"
	"strings"
)

var (
	// ErrCallerNotAllowed is returned when the caller has no matching policy rule.
	ErrCallerNotAllowed = errors.New("caller is not allowed")
	// ErrRepositoryNotAllowed is returned when the request targets a repository
	// that the caller's policy does not permit.
	ErrRepositoryNotAllowed = errors.New("repository is not allowed")
	// ErrPermissionNotAllowed is returned when the request asks for a permission
	// that the caller's policy does not permit.
	ErrPermissionNotAllowed = errors.New("permission is not allowed")
)

// Caller identifies a Kubernetes workload allowed to request tokens.
type Caller struct {
	Namespace      string `yaml:"namespace" json:"namespace"`
	ServiceAccount string `yaml:"service_account" json:"serviceAccount"`
}

// Allow is a single owner/repositories/permissions grant for a caller.
type Allow struct {
	Owner string `yaml:"owner" json:"owner"`
	// Repositories is a list of repository names, or ["*"] for all repositories
	// of the owner. An empty list means all repositories.
	Repositories   []string          `yaml:"repositories,omitempty" json:"repositories,omitempty"`
	MaxPermissions map[string]string `yaml:"max_permissions" json:"maxPermissions"`
}

// Rule binds a caller to a set of grants.
type Rule struct {
	Caller Caller  `yaml:"caller" json:"caller"`
	Allow  []Allow `yaml:"allow" json:"allow"`
}

// Policy is the complete authorization policy. Access is denied by default.
type Policy struct {
	Rules []Rule `yaml:"rules" json:"rules"`
}

// Validate checks the policy for structural errors.
func (p *Policy) Validate() error {
	if p == nil || len(p.Rules) == 0 {
		return errors.New("policy must contain at least one rule")
	}
	for i := range p.Rules {
		rule := &p.Rules[i]
		if strings.TrimSpace(rule.Caller.Namespace) == "" || strings.TrimSpace(rule.Caller.ServiceAccount) == "" {
			return fmt.Errorf("policy rule %d: caller namespace and service_account are required", i)
		}
		if len(rule.Allow) == 0 {
			return fmt.Errorf("policy rule %d: at least one allow entry is required", i)
		}
		for j := range rule.Allow {
			allow := &rule.Allow[j]
			if strings.TrimSpace(allow.Owner) == "" {
				return fmt.Errorf("policy rule %d allow %d: owner is required", i, j)
			}
			if len(allow.MaxPermissions) == 0 {
				return fmt.Errorf("policy rule %d allow %d: max_permissions is required", i, j)
			}
			normalized := map[string]string{}
			for key, level := range allow.MaxPermissions {
				k := normalizeKey(key)
				v := strings.ToLower(strings.TrimSpace(level))
				if k == "" {
					return fmt.Errorf("policy rule %d allow %d: empty permission name", i, j)
				}
				if v != "read" && v != "write" {
					return fmt.Errorf("policy rule %d allow %d: permission %q must be read or write", i, j, key)
				}
				normalized[k] = v
			}
			allow.MaxPermissions = normalized
			for k, repo := range allow.Repositories {
				if strings.TrimSpace(repo) == "" {
					return fmt.Errorf("policy rule %d allow %d: empty repository name", i, j)
				}
				allow.Repositories[k] = strings.TrimSpace(repo)
			}
		}
	}
	return nil
}

// Authorize evaluates the policy for a caller and a token request.
//
// The returned permissions are normalized and always non-empty. When the
// request omits permissions, it defaults to read-only contents access.
func (p *Policy) Authorize(caller Caller, owner, repository string, repositories []string, permissions map[string]string) (map[string]string, error) {
	perms := NormalizePermissions(permissions)
	for name, level := range perms {
		if level != "read" && level != "write" {
			return nil, fmt.Errorf("%w: %s=%s", ErrPermissionNotAllowed, name, level)
		}
	}

	for i := range p.Rules {
		rule := &p.Rules[i]
		if rule.Caller.Namespace != caller.Namespace || rule.Caller.ServiceAccount != caller.ServiceAccount {
			continue
		}
		for j := range rule.Allow {
			allow := &rule.Allow[j]
			if allow.Owner != "*" && !strings.EqualFold(allow.Owner, owner) {
				continue
			}
			if err := checkRepositories(allow.Repositories, repository, repositories); err != nil {
				return nil, err
			}
			if err := checkPermissions(allow.MaxPermissions, perms); err != nil {
				return nil, err
			}
			return perms, nil
		}
	}
	return nil, ErrCallerNotAllowed
}

// NormalizePermissions returns a copy of permissions with normalized keys and
// values. Empty input defaults to {"contents": "read"}.
func NormalizePermissions(permissions map[string]string) map[string]string {
	if len(permissions) == 0 {
		return map[string]string{"contents": "read"}
	}
	out := make(map[string]string, len(permissions))
	for key, level := range permissions {
		out[normalizeKey(key)] = strings.ToLower(strings.TrimSpace(level))
	}
	return out
}

func normalizeKey(key string) string {
	return strings.ToLower(strings.TrimSpace(key))
}

func checkRepositories(allowed []string, repository string, narrowed []string) error {
	allowedSet := map[string]struct{}{}
	allowAll := len(allowed) == 0
	for _, name := range allowed {
		if name == "*" {
			allowAll = true
			continue
		}
		allowedSet[strings.ToLower(name)] = struct{}{}
	}

	if !allowAll {
		if _, ok := allowedSet[strings.ToLower(repository)]; !ok {
			return fmt.Errorf("%w: %s", ErrRepositoryNotAllowed, repository)
		}
	}
	for _, name := range narrowed {
		if !allowAll {
			if _, ok := allowedSet[strings.ToLower(name)]; !ok {
				return fmt.Errorf("%w: %s", ErrRepositoryNotAllowed, name)
			}
		}
	}
	return nil
}

func checkPermissions(max map[string]string, requested map[string]string) error {
	for name, level := range requested {
		maxLevel, ok := max[name]
		if !ok {
			return fmt.Errorf("%w: %s", ErrPermissionNotAllowed, name)
		}
		if level == "write" && maxLevel != "write" {
			return fmt.Errorf("%w: %s=%s (max %s)", ErrPermissionNotAllowed, name, level, maxLevel)
		}
	}
	return nil
}
