package im

import (
	"context"

	"github.com/Tencent/WeKnora/internal/logger"
)

// beginIMAttachmentProgress acknowledges slow downloads and vision calls before
// they consume WeCom's initial callback deadline. QA reuses the same bubble.
func beginIMAttachmentProgress(ctx context.Context, msg *IncomingMessage, channel *IMChannel, adapter Adapter) func() {
	noop := func() {}
	if msg.Platform != PlatformWeCom || channel.OutputMode == "full" ||
		(msg.MessageType != MessageTypeImage && msg.MessageType != MessageTypeFile && len(msg.Attachments) == 0) {
		return noop
	}
	streamer, ok := adapter.(StreamSender)
	if !ok {
		return noop
	}
	streamID, err := streamer.StartStream(ctx, msg)
	if err != nil {
		logger.Warnf(ctx, "[IM] Attachment progress start failed: %v", err)
		return noop
	}
	if err := streamer.UpdateStreamContent(ctx, msg, streamID, "正在读取图片或附件，请稍候…"); err != nil {
		_ = streamer.EndStream(imOutboundContext(ctx), msg, streamID)
		logger.Warnf(ctx, "[IM] Attachment progress update failed: %v", err)
		return noop
	}
	msg.replyStreamID = streamID
	return func() {
		if msg.replyStreamID == "" {
			return
		}
		outCtx := imOutboundContext(ctx)
		if err := streamer.FinalizeStream(outCtx, msg, streamID, "❌ 无法读取此附件，请重试或改用文字描述。"); err != nil {
			logger.Warnf(ctx, "[IM] Attachment failure reply failed: %v", err)
		}
		if err := streamer.EndStream(outCtx, msg, streamID); err != nil {
			logger.Warnf(ctx, "[IM] Attachment progress cleanup failed: %v", err)
		}
		msg.replyStreamID = ""
	}
}

func startIMReplyStream(ctx context.Context, msg *IncomingMessage, streamer StreamSender) (string, error) {
	if msg.replyStreamID != "" {
		streamID := msg.replyStreamID
		msg.replyStreamID = ""
		return streamID, nil
	}
	return streamer.StartStream(ctx, msg)
}
