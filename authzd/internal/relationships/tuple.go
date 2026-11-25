package relationships

import (
	"fmt"
	"strings"
)

// ValidateTuple checks the tuple syntax before it reaches OpenFGA. The store
// rejects malformed tuples too, but a snapshot is applied as one unit, so a
// single bad entry must fail the whole snapshot instead of silently dropping a
// grant.
func ValidateTuple(tuple Tuple) error {
	if err := validateObjectRef(tuple.Object); err != nil {
		return fmt.Errorf("object %q: %w", tuple.Object, err)
	}
	if err := validateRelation(tuple.Relation); err != nil {
		return fmt.Errorf("relation %q: %w", tuple.Relation, err)
	}
	user, userset, hasUserset := strings.Cut(tuple.User, "#")
	if err := validateObjectRef(user); err != nil {
		return fmt.Errorf("user %q: %w", tuple.User, err)
	}
	if hasUserset {
		if err := validateRelation(userset); err != nil {
			return fmt.Errorf("user %q: %w", tuple.User, err)
		}
	}
	return nil
}

func validateObjectRef(ref string) error {
	resourceType, id, found := strings.Cut(ref, ":")
	if !found || id == "" || strings.Contains(id, ":") {
		return fmt.Errorf("want <type>:<id>")
	}
	if err := validateName(resourceType); err != nil {
		return err
	}
	for _, r := range id {
		if isNameRune(r) || strings.ContainsRune("|/", r) {
			continue
		}
		return fmt.Errorf("unexpected character %q in identifier", r)
	}
	return nil
}

func validateRelation(relation string) error {
	if err := validateName(relation); err != nil {
		return fmt.Errorf("want an identifier")
	}
	return nil
}

func validateName(value string) error {
	if value == "" {
		return fmt.Errorf("name is empty")
	}
	first := rune(value[0])
	if !(first >= 'a' && first <= 'z') && !(first >= 'A' && first <= 'Z') {
		return fmt.Errorf("name must start with a letter")
	}
	for _, r := range value {
		if !isNameRune(r) {
			return fmt.Errorf("unexpected character %q in name", r)
		}
	}
	return nil
}

func isNameRune(r rune) bool {
	return (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || strings.ContainsRune("_.-", r)
}
