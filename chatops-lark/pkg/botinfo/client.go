package botinfo

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	lark "github.com/larksuite/oapi-sdk-go/v3"
	larkcore "github.com/larksuite/oapi-sdk-go/v3/core"
)

// Lark API endpoints
const botInfoPathURL = "/open-apis/bot/v3/info"

// BotInfoResponse represents the response from the bot info API
type BotInfoResponse struct {
	Code int    `json:"code"`
	Msg  string `json:"msg"`
	Bot  struct {
		ActivateStatus int      `json:"activate_status"`
		AppName        string   `json:"app_name"`
		AvatarURL      string   `json:"avatar_url"`
		IPWhiteList    []string `json:"ip_white_list"`
		OpenID         string   `json:"open_id"`
	} `json:"bot"`
}

// GetBotOpenID fetches the bot OpenID from Lark API using app credentials
// Renamed from GetBotInfo and simplified to return only OpenID
func GetBotOpenID(ctx context.Context, appID, appSecret string) (string, error) {
	client := lark.NewClient(appID, appSecret)
	var tenantAccessToken string
	{
		resp, err := client.GetTenantAccessTokenBySelfBuiltApp(ctx, &larkcore.SelfBuiltTenantAccessTokenReq{
			AppID:     appID,
			AppSecret: appSecret,
		})
		if err != nil {
			return "", fmt.Errorf("failed to get tenant access token: %w", err)
		}
		if !resp.Success() {
			return "", fmt.Errorf("failed to get tenant access token, logId: %s, error response: \n%s",
				resp.RequestId(), larkcore.Prettify(resp.CodeError))
		}

		tenantAccessToken = resp.TenantAccessToken
	}

	var botResp BotInfoResponse
	{
		resp, err := client.Get(ctx, botInfoPathURL, nil, larkcore.AccessTokenTypeTenant,
			larkcore.WithTenantAccessToken(tenantAccessToken),
		)
		if err != nil {
			return "", fmt.Errorf("error creating request: %w", err)
		}

		if resp.StatusCode != http.StatusOK {
			return "", fmt.Errorf("unexpected status code: %d", resp.StatusCode)
		}

		if err := json.Unmarshal(resp.RawBody, &botResp); err != nil {
			return "", fmt.Errorf("error parsing response: %w", err)
		}

		if botResp.Code != 0 {
			return "", fmt.Errorf("API error: %s (code: %d)", botResp.Msg, botResp.Code)
		}

		if botResp.Bot.OpenID == "" {
			return "", fmt.Errorf("bot openID is empty in API response from %s", botInfoPathURL)
		}
	}

	return botResp.Bot.OpenID, nil
}
