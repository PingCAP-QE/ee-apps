package impl

import (
	"regexp"

	larksdk "github.com/larksuite/oapi-sdk-go/v3"
)

var (
	reLarkOpenID  = regexp.MustCompile(`^ou_\w+`)
	reLarkUnionID = regexp.MustCompile(`^on_\w+`)
	reLarkChatID  = regexp.MustCompile(`^oc_\w+`)
	reLarkEmail   = regexp.MustCompile(`^\S+@\S+\.\S+$`)
)

const (
	receiveIdTypeOpenID  = "open_id"
	receiveIdTypeUnionID = "union_id"
	receiveIdTypeChatID  = "chat_id"
	receiveIdTypeEmail   = "email"
	receiveIdTypeUserID  = "user_id"
)

func newLarkClient(appID, appSecret string) *larksdk.Client {
	return larksdk.NewClient(appID, appSecret,
		larksdk.WithLogReqAtDebug(true),
		larksdk.WithEnableTokenCache(true),
	)
}

func getLarkReceiverIDType(id string) string {
	switch {
	case reLarkOpenID.MatchString(id):
		return receiveIdTypeOpenID
	case reLarkUnionID.MatchString(id):
		return receiveIdTypeUnionID
	case reLarkChatID.MatchString(id):
		return receiveIdTypeChatID
	case reLarkEmail.MatchString(id):
		return receiveIdTypeEmail
	default:
		return receiveIdTypeUserID
	}
}
