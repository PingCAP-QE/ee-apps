// Command portal-config compiles EE Portal module YAML into a deterministic registry.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

const compilerVersion = "0.2.0"

type document map[string]any

func readYAML(path string) (document, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var value document
	if err := yaml.Unmarshal(b, &value); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	// Normalize yaml.v3's concrete slice/map types so all validation is deterministic.
	normalized, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(normalized, &value); err != nil {
		return nil, err
	}
	if value == nil {
		return nil, fmt.Errorf("%s: empty document", path)
	}
	return value, nil
}

func normalizeModule(value document) document {
	if metadata, ok := value["metadata"].(map[string]any); ok {
		result := document{"id": metadata["name"], "title": metadata["title"], "description": metadata["description"], "icon": metadata["icon"], "tags": metadata["tags"]}
		if spec, ok := value["spec"].(map[string]any); ok {
			for key, item := range spec {
				result[key] = item
			}
		}
		if sources, ok := result["dataSources"].([]any); ok {
			for _, raw := range sources {
				if item, ok := raw.(map[string]any); ok && item["apiUrl"] == nil {
					item["apiUrl"] = item["basePath"]
				}
			}
		}
		return result
	}
	return value
}

func normalizePage(value document) document {
	if metadata, ok := value["metadata"].(map[string]any); ok {
		result := document{"id": metadata["name"], "module": metadata["module"], "title": metadata["title"], "description": metadata["description"]}
		if spec, ok := value["spec"].(map[string]any); ok {
			for key, item := range spec {
				result[key] = item
			}
		}
		return result
	}
	return value
}

func stringValue(value any) string { s, _ := value.(string); return s }

func inspect(value any, path string, dataSources map[string]bool) error {
	switch item := value.(type) {
	case []any:
		for _, child := range item {
			if err := inspect(child, path, dataSources); err != nil {
				return err
			}
		}
	case map[string]any:
		if _, named := item["name"]; named {
			if _, labeled := item["label"]; labeled {
				if !map[string]bool{"request": true, "navigate": true, "confirm": true, "notify": true, "copy": true, "open-link": true, "refresh": true}[stringValue(item["type"])] {
					return fmt.Errorf("%s: unsupported action %q", path, item["type"])
				}
			}
		}
		for key, child := range item {
			for _, forbidden := range []string{"script", "javascript", "headers", "secret", "token"} {
				if key == forbidden {
					return fmt.Errorf("%s: forbidden key %q", path, key)
				}
			}
			if key == "plugin" && stringValue(child) != "core.dynamic-select" {
				return fmt.Errorf("%s: unknown plugin %q", path, child)
			}
			if key == "from" && !map[string]bool{"form": true, "route": true, "query": true, "state": true, "response": true}[stringValue(child)] {
				return fmt.Errorf("%s: unsupported value source %q", path, child)
			}
			if key == "dataSource" && !dataSources[stringValue(child)] {
				return fmt.Errorf("%s: unknown data source %q", path, child)
			}
			if key == "method" && !map[string]bool{"GET": true, "POST": true, "PUT": true, "PATCH": true, "DELETE": true}[stringValue(child)] {
				return fmt.Errorf("%s: unsupported HTTP method %q", path, child)
			}
			if key == "path" {
				p := stringValue(child)
				if p == "" || !strings.HasPrefix(p, "/") || strings.HasPrefix(p, "//") || strings.HasPrefix(strings.ToLower(p), "http:") || strings.HasPrefix(strings.ToLower(p), "https:") {
					return fmt.Errorf("%s: request path must be a relative path", path)
				}
			}
			if err := inspect(child, path, dataSources); err != nil {
				return err
			}
		}
	}
	return nil
}

func validatePage(page document, path, module string, routes map[string]bool) error {
	if stringValue(page["id"]) == "" || stringValue(page["title"]) == "" {
		return fmt.Errorf("%s: id and title are required", path)
	}
	if stringValue(page["module"]) != module {
		return fmt.Errorf("%s: module must be %q", path, module)
	}
	route := stringValue(page["route"])
	if route == "" || !strings.HasPrefix(route, "/") {
		return fmt.Errorf("%s: route must start with '/'", path)
	}
	if routes[route] {
		return fmt.Errorf("%s: duplicate route %q", path, route)
	}
	routes[route] = true
	return nil
}

func compile(input string) ([]byte, error) {
	entries, err := os.ReadDir(input)
	if err != nil {
		return nil, err
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	modules := make([]document, 0)
	sources := make([]any, 0)
	routes := map[string]bool{}
	names := map[string]bool{}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		dir := filepath.Join(input, entry.Name())
		modulePath := filepath.Join(dir, "module.yaml")
		if _, err := os.Stat(modulePath); errors.Is(err, os.ErrNotExist) {
			continue
		}
		raw, err := readYAML(modulePath)
		if err != nil {
			return nil, err
		}
		module := normalizeModule(raw)
		id := stringValue(module["id"])
		title := stringValue(module["title"])
		if id == "" || title == "" {
			return nil, fmt.Errorf("%s: id and title are required", modulePath)
		}
		if names[id] {
			return nil, fmt.Errorf("%s: duplicate module %q", modulePath, id)
		}
		names[id] = true
		dataSources := map[string]bool{}
		if list, ok := module["dataSources"].([]any); ok {
			for _, rawSource := range list {
				item, ok := rawSource.(map[string]any)
				if !ok || stringValue(item["name"]) == "" || stringValue(item["apiUrl"]) == "" {
					return nil, fmt.Errorf("%s: data sources require name and apiUrl", modulePath)
				}
				dataSources[stringValue(item["name"])] = true
			}
		}
		pages := make([]any, 0)
		pageDir := filepath.Join(dir, "pages")
		pageEntries, _ := os.ReadDir(pageDir)
		sort.Slice(pageEntries, func(i, j int) bool { return pageEntries[i].Name() < pageEntries[j].Name() })
		pageNames := map[string]bool{}
		for _, pageEntry := range pageEntries {
			if pageEntry.IsDir() || (!strings.HasSuffix(pageEntry.Name(), ".yaml") && !strings.HasSuffix(pageEntry.Name(), ".yml")) {
				continue
			}
			path := filepath.Join(pageDir, pageEntry.Name())
			rawPage, err := readYAML(path)
			if err != nil {
				return nil, err
			}
			page := normalizePage(rawPage)
			if err := validatePage(page, path, id, routes); err != nil {
				return nil, err
			}
			if err := inspect(page, path, dataSources); err != nil {
				return nil, err
			}
			pageNames[stringValue(page["id"])] = true
			pages = append(pages, page)
			sources = append(sources, page)
		}
		if nav, ok := module["navigation"].([]any); ok {
			for _, rawNav := range nav {
				item, _ := rawNav.(map[string]any)
				if !pageNames[stringValue(item["page"])] {
					return nil, fmt.Errorf("%s: navigation references unknown page", modulePath)
				}
			}
		}
		sources = append(sources, module)
		module["pages"] = pages
		modules = append(modules, module)
	}
	if len(modules) == 0 {
		return nil, fmt.Errorf("%s: no module.yaml files found", input)
	}
	sourceJSON, _ := json.Marshal(sources)
	digest := sha256.Sum256(sourceJSON)
	registry := document{"registryVersion": 1, "compilerVersion": compilerVersion, "minimumRuntimeVersion": "0.1.0", "sourceDigest": hex.EncodeToString(digest[:]), "modules": modules}
	out, _ := json.MarshalIndent(registry, "", "  ")
	return append(out, '\n'), nil
}

func main() {
	input := flag.String("input", "testdata/modules", "module source directory")
	output := flag.String("output", "public/config/registry.json", "registry output path")
	check := flag.Bool("check", false, "verify output is current")
	flag.Parse()
	compiled, err := compile(*input)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if *check {
		existing, readErr := os.ReadFile(*output)
		if readErr != nil || string(existing) != string(compiled) {
			fmt.Fprintf(os.Stderr, "%s is stale; run portal-config\n", *output)
			os.Exit(1)
		}
		fmt.Printf("Registry is up to date (%s)\n", filepath.Base(*output))
		return
	}
	if err := os.MkdirAll(filepath.Dir(*output), 0o755); err != nil {
		panic(err)
	}
	if err := os.WriteFile(*output, compiled, 0o644); err != nil {
		panic(err)
	}
	fmt.Printf("Compiled %s -> %s\n", *input, *output)
}
