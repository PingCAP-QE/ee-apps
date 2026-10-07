/*
Copyright 2025.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	"sigs.k8s.io/yaml"
)

// builderEntry is a single entry of a component's `builders` list in the rendered
// per-component artifacts config (`release-package.yaml`), produced by the
// artifacts repo's gen script. See PingCAP-QE/artifacts packages/packages.yaml.tmpl.
type builderEntry struct {
	If    bool   `json:"if"`
	Image string `json:"image"`
	MacOS *struct {
		Tools map[string]string `json:"tools"`
	} `json:"macos"`
}

type componentConfig struct {
	Builders []builderEntry `json:"builders"`
}

// resolveMacOSTools returns the `macos.tools` map of the single matched builder
// from the rendered per-component artifacts config. It returns nil (no error)
// when no matched builder declares `macos.tools` — the caller then falls back.
func resolveMacOSTools(data []byte) (map[string]string, error) {
	var cfg componentConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse component config: %w", err)
	}
	for i := range cfg.Builders {
		b := cfg.Builders[i]
		if !b.If || b.MacOS == nil {
			continue
		}
		if len(b.MacOS.Tools) == 0 {
			return nil, nil
		}
		return b.MacOS.Tools, nil
	}
	return nil, nil
}

var bareTomlKey = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// renderMiseToml renders a minimal mise.toml containing only the [tools] table,
// with keys sorted for determinism. Empty input yields an empty string.
func renderMiseToml(tools map[string]string) string {
	if len(tools) == 0 {
		return ""
	}
	keys := make([]string, 0, len(tools))
	for k := range tools {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var b strings.Builder
	b.WriteString("[tools]\n")
	for _, k := range keys {
		key := k
		if !bareTomlKey.MatchString(key) {
			key = `"` + strings.ReplaceAll(key, `"`, `\"`) + `"`
		}
		val := strings.ReplaceAll(tools[k], `"`, `\"`)
		fmt.Fprintf(&b, "%s = \"%s\"\n", key, val)
	}
	return b.String()
}
