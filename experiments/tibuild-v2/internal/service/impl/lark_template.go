package impl

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"text/template"

	"github.com/Masterminds/sprig/v3"
	yaml "gopkg.in/yaml.v3"
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
	case len(gitRef) > 7 && gitRef[:7] == "branch/":
		return "https://github.com/" + repo + "/tree/" + gitRef[7:]
	case len(gitRef) > 4 && gitRef[:4] == "tag/":
		return "https://github.com/" + repo + "/tree/" + gitRef[4:]
	case len(gitRef) > 5 && gitRef[:5] == "pull/":
		return "https://github.com/" + repo + "/pull/" + gitRef[5:]
	case len(gitRef) > 7 && gitRef[:7] == "commit/":
		return "https://github.com/" + repo + "/commit/" + gitRef[7:]
	default:
		return ""
	}
}

//go:embed lark_templates/devbuild-notify.yaml.tmpl
var larkTemplateBytes string

// NewLarkCardWithGoTemplate renders the English notification template.
// Go supplies artifact data and escaping helpers.
func NewLarkCardWithGoTemplate(infos *NotificationInfo) (map[string]any, error) {
	var tmpl *template.Template
	funcs := sprig.FuncMap()
	funcs["StatusColor"] = StatusColor
	funcs["StatusEmoji"] = StatusEmoji
	funcs["PipelineStatusEmoji"] = PipelineStatusEmoji
	funcs["GitRefURL"] = gitRefURL
	funcs["Escape"] = escapeCardText
	funcs["Code"] = inlineCardCode
	funcs["Link"] = cardLink
	funcs["ImageSyncURL"] = imageSyncURL
	funcs["ValidHTTPURL"] = validHTTPURL
	var err error
	tmpl, err = template.New("lark").Funcs(funcs).Parse(larkTemplateBytes)
	if err != nil {
		return nil, err
	}
	var out bytes.Buffer
	if err := tmpl.Execute(&out, prepareNotificationCard(infos)); err != nil {
		return nil, err
	}
	var card map[string]any
	if err := yaml.Unmarshal(out.Bytes(), &card); err != nil {
		return nil, err
	}
	return card, nil
}

// NewLarkCardJSON builds a Lark Card JSON 2.0 message.
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
