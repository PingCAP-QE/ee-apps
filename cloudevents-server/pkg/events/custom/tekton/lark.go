package tekton

import (
	"crypto/tls"
	"fmt"
	"net/http"
	"strings"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkcard "github.com/larksuite/oapi-sdk-go/v3/card"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	tektoncloudevent "github.com/tektoncd/pipeline/pkg/reconciler/events/cloudevent"

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
)

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

func newLarkMessage(receiveEmail string, event cloudevents.Event, detailBaseUrl string) (*larkim.CreateMessageReq, error) {
	messageCard := newLarkCard(event.Type(), event.Subject(), event.Source(), detailBaseUrl)
	messageRawStr, err := messageCard.String()
	if err != nil {
		return nil, err
	}

	req := larkim.NewCreateMessageReqBuilder().
		ReceiveIdType(larkim.ReceiveIdTypeEmail).
		Body(
			larkim.NewCreateMessageReqBodyBuilder().
				MsgType(larkim.MsgTypeInteractive).
				ReceiveId(receiveEmail).
				Content(messageRawStr).
				Build(),
		).
		Build()

	return req, nil
}

func newLarkCard(etype, subject, source, baseURL string) *larkcard.MessageCard {
	title := newLarkTitle(etype, subject)
	header := larkcard.NewMessageCardHeader().
		Template(larkCardHeaderTemplates[tektoncloudevent.TektonEventType(etype)]).
		Title(larkcard.NewMessageCardPlainText().Content(title))

	detailLinkAction := larkcard.NewMessageCardAction().Actions([]larkcard.MessageCardActionElement{
		larkcard.NewMessageCardEmbedButton().
			Type(larkcard.MessageCardButtonTypeDefault).
			Text(larkcard.NewMessageCardPlainText().Content("View")).
			Url(newDetailURL(etype, source, baseURL)),
	})

	return larkcard.NewMessageCard().
		Config(larkcard.NewMessageCardConfig().WideScreenMode(true)).
		Header(header).
		Elements([]larkcard.MessageCardElement{
			detailLinkAction,
		})
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
