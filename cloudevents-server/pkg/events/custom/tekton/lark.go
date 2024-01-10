package tekton

import (
	"context"
	"crypto/tls"
	"fmt"
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

	"github.com/PingCAP-QE/ee-apps/cloudevents-server/pkg/config"
)

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
	messageCard := newLarkCard(event.Type(), event.Subject(), event.Source(), detailBaseUrl)
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
