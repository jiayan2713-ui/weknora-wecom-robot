package im

import (
	"context"
	"testing"
)

type mediaProgressAdapter struct {
	*lifecycleTestAdapter
	*recordingStreamSender
}

func TestAttachmentProgressTransfersStreamToQA(t *testing.T) {
	adapter := &mediaProgressAdapter{&lifecycleTestAdapter{}, &recordingStreamSender{}}
	msg := &IncomingMessage{Platform: PlatformWeCom, MessageType: MessageTypeImage}
	cleanup := beginIMAttachmentProgress(context.Background(), msg, &IMChannel{OutputMode: "stream"}, adapter)
	if len(adapter.chunkContents) != 1 || msg.replyStreamID == "" {
		t.Fatal("did not acknowledge attachment before reading")
	}
	id, err := startIMReplyStream(context.Background(), msg, adapter)
	if err != nil || id != "rec-stream-1" || msg.replyStreamID != "" {
		t.Fatal("failed to transfer progress stream")
	}
	cleanup()
	if adapter.ended {
		t.Fatal("cleanup ended a stream now owned by QA")
	}
}
func TestAttachmentProgressClosesOnReadFailure(t *testing.T) {
	adapter := &mediaProgressAdapter{&lifecycleTestAdapter{}, &recordingStreamSender{}}
	msg := &IncomingMessage{Platform: PlatformWeCom, MessageType: MessageTypeFile}
	cleanup := beginIMAttachmentProgress(context.Background(), msg, &IMChannel{OutputMode: "stream"}, adapter)
	cleanup()
	if !adapter.ended || adapter.finalContent == "" || msg.replyStreamID != "" {
		t.Fatal("read failure leaked unfinished progress stream")
	}
}
