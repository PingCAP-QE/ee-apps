package audit

import (
	"bytes"
	"encoding/json"
	"fmt"
	"html/template"

	sprig "github.com/Masterminds/sprig/v3"
	"github.com/go-resty/resty/v2"
	"gopkg.in/yaml.v3"

	_ "embed"
)

//go:embed audit-card.yaml.tmpl
var larkTemplateBytes string

type AuditInfo struct {
	UserEmail string
	Command   string
	Args      []string
}

func RecordAuditMessage(info *AuditInfo, auditWebhook string) error {
	card, err := newLarkCardWithGoTemplate(info)
	if err != nil {
		return err
	}

	res, err := resty.New().R().SetBody(map[string]string{
		"card":     card,
		"msg_type": "interactive",
	}).Post(auditWebhook)
	if err != nil {
		return err
	}

	if !res.IsSuccess() {
		return fmt.Errorf("failed to send audit message: [%d] %s", res.StatusCode(), res.Status())
	}

	return nil
}

func newLarkCardWithGoTemplate(i *AuditInfo) (string, error) {
	tmpl, err := template.New("lark").Funcs(sprig.FuncMap()).Parse(larkTemplateBytes)
	if err != nil {
		return "", err
	}

	tmplResult := new(bytes.Buffer)
	if err := tmpl.Execute(tmplResult, i); err != nil {
		return "", err
	}

	values := make(map[string]any)
	if err := yaml.Unmarshal(tmplResult.Bytes(), &values); err != nil {
		return "", err
	}

	jsonBytes, err := json.Marshal(values)
	if err != nil {
		return "", err
	}

	return string(jsonBytes), nil
}
