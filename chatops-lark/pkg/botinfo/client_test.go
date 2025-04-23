package botinfo

import (
	"context"
	"flag"
	"testing"
	"time"
)

var (
	testAppID     = flag.String("app-id", "", "app id")
	testAppSecret = flag.String("app-secret", "", "app secret")
)


func TestGetBotOpenID(t *testing.T) {
	// You should run it with: go test -run=TestGetBotOpenID/real_test ./pkg/botinfo -app-id <app-id> -app-secret <app-secret>
	t.Run("real test", func(tt *testing.T) {
		flag.Parse()

		if *testAppID == "" || *testAppSecret == "" {
			tt.Skip("app-id and app-secret flags are required")
			return
		}

		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		// Call the new function and handle only openID return value
		openID, err := GetBotOpenID(ctx, *testAppID, *testAppSecret)
		if err != nil {
			tt.Fatal(err)
		}

		tt.Logf("OpenID: %s", openID) // Log only openID

		if openID == "" {
			tt.Fatal("Expected non-empty bot openID")
		}
	})
}
