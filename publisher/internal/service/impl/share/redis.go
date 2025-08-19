package share

import (
	"context"
	"fmt"

	"github.com/go-redis/redis/v8"
)

// QueryStatusFromRedis query status from Redis.
func QueryStatusFromRedis(ctx context.Context, redisClient redis.Cmdable, requestID string) (string, error) {
	status, err := redisClient.Get(ctx, requestID).Result()
	if err != nil {
		if err == redis.Nil {
			return "", fmt.Errorf("request ID not found")
		}
		return "", fmt.Errorf("failed to get status from Redis: %v", err)
	}

	return status, nil
}
