package resolver

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
)

type Catalog struct {
	Version   string            `json:"version"`
	Resources map[string]string `json:"resources"`
}

type StaticCatalog struct {
	version   string
	resources map[string]string
}

// ErrNotFound marks a resource ID that the trusted catalog does not contain.
// It is a decision input, not a dependency failure: an unknown or mismatched ID
// must deny without inventing ownership or returning a server error.
var ErrNotFound = errors.New("resource is not present in the trusted catalog")

type Unavailable struct{}

func (Unavailable) Resolve(context.Context, string, string) (string, error) {
	return "", errors.New("trusted resource catalog is not configured")
}

func (Unavailable) Version() string { return "unconfigured" }

func Parse(data []byte) (*StaticCatalog, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var catalog Catalog
	if err := dec.Decode(&catalog); err != nil {
		return nil, fmt.Errorf("decode resource catalog: %w", err)
	}
	if err := dec.Decode(new(any)); !errors.Is(err, io.EOF) {
		return nil, errors.New("resource catalog must contain exactly one JSON value")
	}
	if strings.TrimSpace(catalog.Version) == "" {
		return nil, errors.New("resource catalog version is required")
	}
	if len(catalog.Resources) == 0 {
		return nil, errors.New("resource catalog must contain at least one trusted resource")
	}
	for ref, object := range catalog.Resources {
		resourceType, id, found := strings.Cut(ref, "/")
		if !found || !validIdentifier(resourceType) || id == "" || strings.Contains(id, "/") || !validObject(object) {
			return nil, fmt.Errorf("invalid resource catalog entry %q", ref)
		}
		objectType, _, _ := strings.Cut(object, ":")
		if resourceType != objectType {
			return nil, fmt.Errorf("resource catalog entry %q resolves to mismatched object type", ref)
		}
	}
	resources := make(map[string]string, len(catalog.Resources))
	for key, value := range catalog.Resources {
		resources[key] = value
	}
	return &StaticCatalog{version: catalog.Version, resources: resources}, nil
}

func (c *StaticCatalog) Resolve(_ context.Context, resourceType, id string) (string, error) {
	if c == nil || resourceType == "" || id == "" {
		return "", errors.New("resource type and ID are required")
	}
	object, ok := c.resources[resourceType+"/"+id]
	if !ok {
		return "", fmt.Errorf("%w: %s/%s", ErrNotFound, resourceType, id)
	}
	return object, nil
}

func (c *StaticCatalog) Version() string {
	if c == nil {
		return "unconfigured"
	}
	return c.version
}

func validObject(object string) bool {
	typ, id, ok := strings.Cut(object, ":")
	if !ok || !validIdentifier(typ) || id == "" {
		return false
	}
	for _, r := range id {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || strings.ContainsRune("_.:@/-", r) {
			continue
		}
		return false
	}
	return true
}

func validIdentifier(value string) bool {
	if value == "" || !((value[0] >= 'A' && value[0] <= 'Z') || (value[0] >= 'a' && value[0] <= 'z')) {
		return false
	}
	for _, r := range value[1:] {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || strings.ContainsRune("_.-", r) {
			continue
		}
		return false
	}
	return true
}
