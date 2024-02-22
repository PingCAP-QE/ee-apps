package tekton

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"text/template"
	"time"

	"github.com/Masterminds/sprig/v3"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/protocol"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog/log"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
	"gopkg.in/yaml.v3"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"knative.dev/pkg/apis"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"

	_ "embed"
)

//go:embed lark_templates/tekton-run-notify.yaml.tmpl
var larkTemplateBytes string

// receiver formats:
// - Open ID: ou_......
// - Union ID: on_......
// - Chat ID: oc_......
// - Email: some email address.
// - User ID: I do not know
var (
	reLarkOpenID  = regexp.MustCompile(`^ou_\w+`)
	reLarkUnionID = regexp.MustCompile(`^on_\w+`)
	reLarkChatID  = regexp.MustCompile(`^oc_\w+`)
	reLarkEmail   = regexp.MustCompile(`^\S+@\S+\.\S+$`)
)

func getReceiverIDType(id string) string {
	switch {
	case reLarkOpenID.MatchString(id):
		return larkim.ReceiveIdTypeOpenId
	case reLarkUnionID.MatchString(id):
		return larkim.ReceiveIdTypeUnionId
	case reLarkChatID.MatchString(id):
		return larkim.ReceiveIdTypeChatId
	case reLarkEmail.MatchString(id):
		return larkim.ReceiveIdTypeEmail
	default:
		return larkim.ReceiveIdTypeUserId
	}
}

func newMessageReq(receiver string, messageRawStr string) *larkim.CreateMessageReq {
	return larkim.NewCreateMessageReqBuilder().
		ReceiveIdType(getReceiverIDType(receiver)).
		Body(
			larkim.NewCreateMessageReqBodyBuilder().
				MsgType(larkim.MsgTypeInteractive).
				ReceiveId(receiver).
				Content(messageRawStr).
				Build(),
		).
		Build()
}

func newLarkClient(cfg config.Lark) *lark.Client {
	// Disable certificate verification
	tr := &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
	}
	httpClient := &http.Client{Transport: tr}

	return lark.NewClient(cfg.AppID, cfg.AppSecret,
		lark.WithLogReqAtDebug(true),
		lark.WithEnableTokenCache(true),
		lark.WithHttpClient(httpClient),
	)
}

func sendLarkMessages(client *lark.Client, receivers []string, event cloudevents.Event, detailBaseUrl string) protocol.Result {
	createMsgReqs, err := newLarkMessages(receivers, event, detailBaseUrl)
	if err != nil {
		log.Error().Err(err).Msg("compose lark message failed")
		return cloudevents.NewHTTPResult(http.StatusInternalServerError, "compose lark message failed: %v", err)
	}

	for _, createMsgReq := range createMsgReqs {
		resp, err := client.Im.Message.Create(context.Background(), createMsgReq)
		if err != nil {
			log.Error().Err(err).Msg("send lark message failed")
			return cloudevents.NewHTTPResult(http.StatusInternalServerError, "send lark message failed: %v", err)
		}

		if !resp.Success() {
			log.Error().Msg(string(resp.RawBody))
			return cloudevents.ResultNACK
		}

		log.Info().
			Str("request-id", resp.RequestId()).
			Str("message-id", *resp.Data.MessageId).
			Msg("send lark message successfully.")
	}

	return cloudevents.ResultACK
}

func newLarkMessages(receivers []string, event cloudevents.Event, detailBaseUrl string) ([]*larkim.CreateMessageReq, error) {
	messageRawStr, err := newLarkCardWithGoTemplate(event, detailBaseUrl)
	if err != nil {
		return nil, err
	}

	var reqs []*larkim.CreateMessageReq
	for _, r := range receivers {
		reqs = append(reqs, newMessageReq(r, messageRawStr))
	}

	return reqs, nil
}

func extractLarkInfosFromEvent(event cloudevents.Event, baseURL string) (*cardMessageInfos, error) {
	var data tektoncloudevent.TektonCloudEventData
	if err := event.DataAs(&data); err != nil {
		return nil, err
	}

	ret := cardMessageInfos{
		Title:         newLarkTitle(event.Type(), event.Subject()),
		TitleTemplate: larkCardHeaderTemplates[tektoncloudevent.TektonEventType(event.Type())],
		ViewURL:       newDetailURL(event.Type(), event.Source(), baseURL),
	}

	var startTime, endTime *metav1.Time
	switch {
	case data.PipelineRun != nil:
		startTime = data.PipelineRun.Status.StartTime
		endTime = data.PipelineRun.Status.CompletionTime
		if data.PipelineRun.Status.GetCondition(apis.ConditionSucceeded).IsFalse() {
			ret.RerunURL = fmt.Sprintf("tkn -n %s pipeline start %s --use-pipelinerun %s",
				data.PipelineRun.Namespace, data.PipelineRun.Spec.PipelineRef.Name, data.PipelineRun.Name)
		}

		if results := data.PipelineRun.Status.PipelineResults; len(results) > 0 {
			var parts []string
			for _, r := range results {
				parts = append(parts, fmt.Sprintf("%s:", r.Name), r.Value)
				ret.Results = append(ret.Results, [2]string{r.Name, r.Value})
			}
		}
	case data.TaskRun != nil:
		startTime = data.TaskRun.Status.StartTime
		endTime = data.TaskRun.Status.CompletionTime
		if data.TaskRun.Status.GetCondition(apis.ConditionSucceeded).IsFalse() {
			ret.RerunURL = fmt.Sprintf("tkn -n %s task start %s --use-taskrun %s",
				data.TaskRun.Namespace, data.TaskRun.Spec.TaskRef.Name, data.TaskRun.Name)
		}

		if results := data.TaskRun.Status.TaskRunResults; len(results) > 0 {
			var parts []string
			for _, r := range results {
				v, _ := r.Value.MarshalJSON()
				parts = append(parts, fmt.Sprintf("**%s**:", r.Name), string(v), "---")
				ret.Results = append(ret.Results, [2]string{r.Name, string(v)})
			}
		}
	case data.Run != nil:
		startTime = data.Run.Status.StartTime
		endTime = data.Run.Status.CompletionTime

		if results := data.Run.Status.Results; len(results) > 0 {
			var parts []string
			for _, r := range results {
				parts = append(parts, fmt.Sprintf("**%s**:", r.Name), r.Value, "---")
				ret.Results = append(ret.Results, [2]string{r.Name, r.Value})

			}
		}
	}

	if startTime != nil {
		ret.StartTime = startTime.Format(time.RFC3339)
	}
	if endTime != nil {
		ret.EndTime = endTime.Format(time.RFC3339)
	}
	if startTime != nil && endTime != nil {
		ret.TimeCost = time.Duration(endTime.UnixNano() - startTime.UnixNano()).String()
	}

	return &ret, nil
}

func newLarkCardWithGoTemplate(event cloudevents.Event, baseURL string) (string, error) {
	infos, err := extractLarkInfosFromEvent(event, baseURL)
	if err != nil {
		return "", err
	}

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

func newLarkTitle(etype, subject string) string {
	typeWords := strings.Split(etype, ".")
	var runType, runState string
	if len(typeWords) >= 5 {
		runType = typeWords[3]
		runState = typeWords[4]
	}

	return fmt.Sprintf("%s [%s] %s is %s ", larkCardHeaderEmojis[tektoncloudevent.TektonEventType(etype)], runType, subject, runState)
}

// <dashboard base url>/#/namespaces/<namespace>/<run-type>s/<run-name>
// source: /apis///namespaces/<namespace>//<run-name>
// https://tekton.abc.com/tekton/apis/tekton.dev/v1beta1/namespaces/ee-cd/pipelineruns/auto-compose-multi-arch-image-run-g5hqv
//
//	"source": "/apis///namespaces/ee-cd//build-package-tikv-tikv-linux-9bn55-build-binaries",
func newDetailURL(etype, source, baseURL string) string {
	words := strings.Split(source, "/")
	runName := words[len(words)-1]
	runType := words[len(words)-2]
	runNamespace := words[len(words)-3]

	if runType == "" {
		runType = strings.Split(etype, ".")[3] + "s"
	}

	return fmt.Sprintf("%s/#/namespaces/%s/%s/%s", baseURL, runNamespace, runType, runName)
}
