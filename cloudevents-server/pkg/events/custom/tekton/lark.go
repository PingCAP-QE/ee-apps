package tekton

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"html/template"
	"net/http"
	"regexp"
	"strings"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/protocol"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkcard "github.com/larksuite/oapi-sdk-go/v3/card"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog/log"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"
	"gopkg.in/yaml.v3"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"knative.dev/pkg/apis"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"

	_ "embed"
)

//go:embed lark_templates/pipelinerun-notify.yaml.tmpl
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
	var eventData tektoncloudevent.TektonCloudEventData
	if err := event.DataAs(&eventData); err != nil {
		return nil, err
	}

	messageCard := newLarkCard(event.Type(), event.Subject(), event.Source(), detailBaseUrl, &eventData)
	messageRawStr, err := messageCard.String()
	if err != nil {
		return nil, err
	}

	var reqs []*larkim.CreateMessageReq
	for _, r := range receivers {
		reqs = append(reqs, newMessageReq(r, messageRawStr))
	}

	return reqs, nil
}

func newLarkCard(etype, subject, source, baseURL string, data *tektoncloudevent.TektonCloudEventData) *larkcard.MessageCard {
	title := newLarkTitle(etype, subject)
	header := larkcard.NewMessageCardHeader().
		Template(larkCardHeaderTemplates[tektoncloudevent.TektonEventType(etype)]).
		Title(larkcard.NewMessageCardPlainText().Content(title))

	return larkcard.NewMessageCard().
		Config(larkcard.NewMessageCardConfig().WideScreenMode(true)).
		Header(header).
		Elements(append(newMessageCardFieldFromTektonCloudEventData(data),
			// detail link
			larkcard.NewMessageCardAction().Actions([]larkcard.MessageCardActionElement{
				larkcard.NewMessageCardEmbedButton().
					Type(larkcard.MessageCardButtonTypeDefault).
					Text(larkcard.NewMessageCardPlainText().Content("View")).
					Url(newDetailURL(etype, source, baseURL)),
			})),
		)
}

func newMessageCardFieldFromTektonCloudEventData(data *tektoncloudevent.TektonCloudEventData) []larkcard.MessageCardElement {
	var startTime, endTime *metav1.Time
	var rerunCmd string
	switch {
	case data.PipelineRun != nil:
		startTime = data.PipelineRun.Status.StartTime
		endTime = data.PipelineRun.Status.CompletionTime
		if data.PipelineRun.Status.GetCondition(apis.ConditionSucceeded).IsFalse() {
			rerunCmd = fmt.Sprintf("tkn -n %s pipeline start %s --use-pipelinerun %s",
				data.PipelineRun.Namespace, data.PipelineRun.Spec.PipelineRef.Name, data.PipelineRun.Name)
		}
	case data.TaskRun != nil:
		startTime = data.TaskRun.Status.StartTime
		endTime = data.TaskRun.Status.CompletionTime
		if data.TaskRun.Status.GetCondition(apis.ConditionSucceeded).IsFalse() {
			rerunCmd = fmt.Sprintf("tkn -n %s task start %s --use-taskrun %s",
				data.TaskRun.Namespace, data.TaskRun.Spec.TaskRef.Name, data.TaskRun.Name)
		}
	case data.Run != nil:
		startTime = data.Run.Status.StartTime
		endTime = data.Run.Status.CompletionTime
	}

	var ret []larkcard.MessageCardElement
	var infoFileds []*larkcard.MessageCardField
	if startTime != nil {
		content := fmt.Sprintf(`**Start time:** %s`, startTime.GoString())
		infoFileds = append(infoFileds,
			larkcard.NewMessageCardField().IsShort(true).Text(larkcard.NewMessageCardLarkMd().Content(content)))
	}
	if endTime != nil {
		content := fmt.Sprintf(`**End time:** %s`, endTime.GoString())
		infoFileds = append(infoFileds,
			larkcard.NewMessageCardField().IsShort(true).Text(larkcard.NewMessageCardLarkMd().Content(content)))
	}
	if startTime != nil && endTime != nil {
		content := fmt.Sprintf(`**Time cost:** %ds`, endTime.Unix()-startTime.Unix())
		infoFileds = append(infoFileds,
			larkcard.NewMessageCardField().IsShort(true).Text(larkcard.NewMessageCardLarkMd().Content(content)))
	}

	ret = append(ret, larkcard.NewMessageCardDiv().Fields(infoFileds))

	if rerunCmd != "" {
		content := fmt.Sprintf(`**Rerun command:** %s`, rerunCmd)
		runRunFileds := []*larkcard.MessageCardField{
			larkcard.NewMessageCardField().Text(larkcard.NewMessageCardLarkMd().Content(content)),
		}

		ret = append(ret, larkcard.NewMessageCardHr(), larkcard.NewMessageCardDiv().Fields(runRunFileds))
	}

	return ret
}

func newLarkCardWithGoTemplate(etype, subject, source, baseURL string) *larkcard.MessageCard {
	// todo: replace with go template
	tmpl, err := template.New("lark").Parse(larkTemplateBytes)
	if err != nil {
		return nil
	}

	tmplResult := new(bytes.Buffer)
	if err := tmpl.Execute(tmplResult, nil); err != nil {
		return nil
	}

	values := make(map[string]interface{})
	if err := yaml.Unmarshal(tmplResult.Bytes(), &values); err != nil {
		return nil
	}

	var yamlBytes []byte
	data := make(map[string]interface{})
	if err := yaml.Unmarshal(yamlBytes, data); err != nil {
		return nil
	}

	jsonBytes, err := json.Marshal(data)
	if err != nil {
		return nil
	}

	ret := larkcard.NewMessageCard()
	if err := json.Unmarshal(jsonBytes, ret); err != nil {
		return nil
	}

	return ret
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
