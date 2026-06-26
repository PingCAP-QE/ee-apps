package impl

import (
	"bytes"
	"encoding/json"
	"text/template"

	"github.com/Masterminds/sprig/v3"
	yaml "gopkg.in/yaml.v3"

	_ "embed"
)

//go:embed lark_templates/devbuild-notify.yaml.tmpl
var larkTemplateBytes string

// NewLarkCardWithGoTemplate builds a Lark interactive card JSON from the template.
func NewLarkCardWithGoTemplate(infos *NotificationInfo) (map[string]any, error) {
	tmpl, err := template.New("lark").Funcs(sprig.FuncMap()).Parse(larkTemplateBytes)
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
