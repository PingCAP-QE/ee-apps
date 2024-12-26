package tekton

import (
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"strings"
	"text/template"

	"github.com/Masterminds/sprig/v3"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/cloudevents/sdk-go/v2/protocol"
	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/rs/zerolog/log"
	"gopkg.in/yaml.v3"

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

func composeAndSendLarkMessages(client *lark.Client, receivers []string, infos *cardMessageInfos) protocol.Result {
	createMsgReqs, err := composeLarkMessages(receivers, infos)
	if err != nil {
		log.Error().Err(err).Msg("compose lark message failed")
		return cloudevents.NewReceipt(true, "compose lark message failed: %v", err)
	}

	for _, createMsgReq := range createMsgReqs {
		failedAck := sendLarkMessage(client, createMsgReq)
		if failedAck != nil {
			return failedAck
		}
	}

	return cloudevents.ResultACK
}

func sendLarkMessage(client *lark.Client, createMsgReq *larkim.CreateMessageReq) error {
	resp, err := client.Im.Message.Create(context.Background(), createMsgReq)
	if err != nil {
		log.Error().Err(err).Msg("send lark message failed")
		return cloudevents.NewReceipt(true, "send lark message failed: %v", err)
	}

	if !resp.Success() {
		log.Error().Msg(string(resp.RawBody))
		return cloudevents.ResultNACK
	}

	log.Info().
		Str("request-id", resp.RequestId()).
		Str("message-id", *resp.Data.MessageId).
		Msg("send lark message successfully.")
	return nil
}

func composeLarkMessages(receivers []string, infos *cardMessageInfos) ([]*larkim.CreateMessageReq, error) {
	if infos == nil {
		return nil, nil
	}

	messageRawStr, err := newLarkCardWithGoTemplate(infos)
	if err != nil {
		return nil, err
	}

	var reqs []*larkim.CreateMessageReq
	for _, r := range receivers {
		reqs = append(reqs, newMessageReq(r, messageRawStr))
	}

	return reqs, nil
}

func newMessageReq(receiver string, messageRawStr string) *larkim.CreateMessageReq {
	return larkim.NewCreateMessageReqBuilder().
		ReceiveIdType(getReceiverIDType(receiver)).
		Body(larkim.NewCreateMessageReqBodyBuilder().
			MsgType(larkim.MsgTypeInteractive).
			ReceiveId(receiver).
			Content(messageRawStr).
			Uuid(newLarkMsgSHA1Sum(receiver, messageRawStr)).
			Build()).
		Build()
}

func newLarkCardWithGoTemplate(infos *cardMessageInfos) (string, error) {
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

	return fmt.Sprintf("%s [%s] %s is %s ", headerEmoji(etype), runType, subject, runState)
}

func newLarkMsgSHA1Sum(receiver, content string) string {
	h := sha1.New()
	io.WriteString(h, receiver)
	io.WriteString(h, content)

	return hex.EncodeToString(h.Sum(nil))
}
