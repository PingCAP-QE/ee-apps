package tekton

import "os"

var (
	larkAppID     = os.Getenv("LARK_APP_ID")
	larkAppSecret = os.Getenv("LARK_APP_SECRET")
	receiver      = os.Getenv("LARK_RECEIVER")
	baseURL       = os.Getenv("LINK_BASE_URL")
)
