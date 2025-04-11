package impl

import (
	"bytes"
	"encoding/json"
	"text/template"

	"github.com/Masterminds/sprig/v3"
	"gopkg.in/yaml.v3"

	_ "embed"
)

//go:embed lark_templates/tiup-publish-failure-notify.yaml.tmpl
var larkTemplateBytes string

func newLarkCardWithGoTemplate(infos any) (string, error) {
	tmpl, err := template.New("lark").Funcs(sprig.FuncMap()).Parse(larkTemplateBytes)
	if err != nil {
		return "", err
	}

	tmplResult := new(bytes.Buffer)
	if err := tmpl.Execute(tmplResult, infos); err != nil {
		return "", err
	}

	values := make(map[string]interface{})
	if err := yaml.Unmarshal(tmplResult.Bytes(), &values); err != nil {
		return "", err
	}

	jsonBytes, err := json.Marshal(values)
	if err != nil {
		return "", err
	}

	return string(jsonBytes), nil
}

type failureNotifyInfo struct {
	Title         string
	RerunCommands string
	FailedMessage string
	Params        [][2]string // key-value pairs.
}
