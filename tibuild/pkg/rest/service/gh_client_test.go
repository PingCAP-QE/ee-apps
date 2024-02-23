package service

import (
	"context"
	"os"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestGetHash(t *testing.T) {
	if os.Getenv("TEST_FANOUT") == "" {
		t.Skip("Skipping send event")
	}
	token := os.Getenv("GHTOKEN")
	hash, err := NewGHClient(token).GetHash(context.TODO(), RepoPd, "branch/master")
	require.NoError(t, err)
	require.NotEmpty(t, hash)
}

func TestGetHashSha1(t *testing.T) {
	s := "754095a9f460dcf31f053045cfedfb00b9ad8e81"
	hash, err := NewGHClient("").GetHash(context.TODO(), RepoPd, s)
	require.NoError(t, err)
	require.Equal(t, s, hash)
}
