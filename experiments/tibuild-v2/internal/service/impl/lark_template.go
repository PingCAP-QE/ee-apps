package impl

import (
	"bytes"
	"encoding/json"
	"text/template"

	"github.com/Masterminds/sprig/v3"
	yaml "gopkg.in/yaml.v3"

	_ "embed"
)

// isHex checks if a string is a valid 40-character hex commit SHA.
func isHex(s string) bool {
	if len(s) != 40 {
		return false
	}
	for i := range s {
		switch {
		case s[i] >= '0' && s[i] <= '9':
		case s[i] >= 'a' && s[i] <= 'f':
		case s[i] >= 'A' && s[i] <= 'F':
		default:
			return false
		}
	}
	return true
}

// gitRefURL builds a clickable GitHub URL from a git ref string and repo.
func gitRefURL(gitRef, repo string) string {
	if repo == "" || gitRef == "" {
		return ""
	}
	switch {
	case isHex(gitRef):
		return "https://github.com/" + repo + "/commit/" + gitRef
	case len(gitRef) > 7 && gitRef[:7] == "branch/":
		return "https://github.com/" + repo + "/tree/" + gitRef[7:]
	case len(gitRef) > 4 && gitRef[:4] == "tag/":
		return "https://github.com/" + repo + "/tree/" + gitRef[4:]
	case len(gitRef) > 5 && gitRef[:5] == "pull/":
		return "https://github.com/" + repo + "/pull/" + gitRef[5:]
	case len(gitRef) > 7 && gitRef[:7] == "commit/":
		sha := gitRef[7:]
		if isHex(sha) {
			return "https://github.com/" + repo + "/commit/" + sha
		}
		return ""
	default:
		return ""
	}
}

//go:embed lark_templates/devbuild-notify.yaml.tmpl
var larkTemplateBytes string

// NewLarkCardWithGoTemplate builds a Lark interactive card JSON from the template.
func NewLarkCardWithGoTemplate(infos *NotificationInfo) (map[string]any, error) {
	funcMap := sprig.FuncMap()
	funcMap["StatusColor"] = StatusColor
	funcMap["StatusEmoji"] = StatusEmoji
	funcMap["PipelineStatusEmoji"] = PipelineStatusEmoji
	funcMap["GitRefURL"] = gitRefURL
	tmpl, err := template.New("lark").Funcs(funcMap).Parse(larkTemplateBytes)
	if err != nil {
		return nil, err
	}

	tmplResult := new(bytes.Buffer)
	if err := tmpl.Execute(tmplResult, infos); err != nil {
		return nil, err
	}

	values := make(map[string]any)
	if err := yaml.Unmarshal(tmplResult.Bytes(), &values); err != nil {
		return nil, err
	}

	return values, nil
}

// NewLarkCardJSON builds a Lark interactive card JSON string from the template.
func NewLarkCardJSON(infos *NotificationInfo) (string, error) {
	values, err := NewLarkCardWithGoTemplate(infos)
	if err != nil {
		return "", err
	}

	jsonBytes, err := json.Marshal(values)
	if err != nil {
		return "", err
	}

	return string(jsonBytes), nil
}
