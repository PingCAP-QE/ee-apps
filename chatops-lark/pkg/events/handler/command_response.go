package handler

import (
	"context"
	"fmt"
)

const (
	ctxKeyCommandReply        = "command.reply"
	ctxKeyCommandResponseMeta = "command.response.meta"
)

type commandReplyFunc func(status, message string) error

type commandResponseMeta struct {
	Status string
}

func withCommandReply(ctx context.Context, reply commandReplyFunc) context.Context {
	return context.WithValue(ctx, ctxKeyCommandReply, reply)
}

func withCommandResponseMeta(ctx context.Context, meta *commandResponseMeta) context.Context {
	return context.WithValue(ctx, ctxKeyCommandResponseMeta, meta)
}

func sendCommandReply(ctx context.Context, status, message string) error {
	reply, ok := ctx.Value(ctxKeyCommandReply).(commandReplyFunc)
	if !ok || reply == nil {
		return fmt.Errorf("command reply hook not found in context")
	}

	return reply(status, message)
}

func hasCommandReply(ctx context.Context) bool {
	reply, ok := ctx.Value(ctxKeyCommandReply).(commandReplyFunc)
	return ok && reply != nil
}

func setCommandResponseStatus(ctx context.Context, status string) {
	if status == "" {
		return
	}

	meta, ok := ctx.Value(ctxKeyCommandResponseMeta).(*commandResponseMeta)
	if !ok || meta == nil {
		return
	}

	meta.Status = status
}
