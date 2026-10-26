package im

import (
	"archive/zip"
	"bytes"
	"context"
	"io"
	"strings"
	"testing"
)

func TestAttachmentFilenameFromDecryptedContent(t *testing.T) {
	for _, item := range []struct{ path, ext string }{{"word/document.xml", "docx"}, {"xl/workbook.xml", "xlsx"}, {"ppt/presentation.xml", "pptx"}} {
		var data bytes.Buffer
		w := zip.NewWriter(&data)
		entry, _ := w.Create(item.path)
		entry.Write([]byte("<test/>"))
		w.Close()
		got, err := resolveIMAttachmentFileName("message-id", data.Bytes())
		if err != nil || got != "message-id."+item.ext {
			t.Fatalf("%s: %q %v", item.ext, got, err)
		}
	}
	for _, item := range []struct {
		name string
		data []byte
		want string
	}{
		{"id", []byte("%PDF-1.7\n"), "id.pdf"},
		{"id", []byte("测试正文 QUARTZ-9361"), "id.txt"},
		{"image.png", []byte{0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 'J', 'F', 'I', 'F', 0, 1}, "image.jpg"},
	} {
		got, err := resolveIMAttachmentFileName(item.name, item.data)
		if err != nil || got != item.want {
			t.Fatalf("%s: %q %v", item.name, got, err)
		}
	}
	if _, err := resolveIMAttachmentFileName("id", []byte{0, 1, 2, 3}); err == nil {
		t.Fatal("invented suffix for unknown binary")
	}
}

type multiMediaAdapter struct {
	*lifecycleTestAdapter
	keys []string
}

func (a *multiMediaAdapter) DownloadFile(_ context.Context, msg *IncomingMessage) (io.ReadCloser, string, error) {
	a.keys = append(a.keys, msg.Extra["aes_key"])
	return io.NopCloser(strings.NewReader("content-" + msg.FileKey)), msg.FileName, nil
}
func TestPrepareMixedAttachmentsUsesEachResource(t *testing.T) {
	adapter := &multiMediaAdapter{lifecycleTestAdapter: &lifecycleTestAdapter{}}
	msg := &IncomingMessage{MessageType: MessageTypeImage, Content: "read both", Attachments: []IncomingAttachment{
		{MessageType: MessageTypeFile, FileKey: "first", FileName: "first.txt", Extra: map[string]string{"aes_key": "key-1"}},
		{MessageType: MessageTypeFile, FileKey: "second", FileName: "second.txt", Extra: map[string]string{"aes_key": "key-2"}},
	}}
	parsed, _, downloaded, err := (&Service{}).prepareIMAttachments(context.Background(), msg, adapter)
	if err != nil || len(parsed) != 2 || len(downloaded) != 2 {
		t.Fatalf("%+v %v", parsed, err)
	}
	if parsed[0].Content != "content-first" || parsed[1].Content != "content-second" || strings.Join(adapter.keys, ",") != "key-1,key-2" {
		t.Fatalf("wrong mapping: %+v %+v", parsed, adapter.keys)
	}
	msg.Attachments = make([]IncomingAttachment, 6)
	if _, _, _, err := (&Service{}).prepareIMAttachments(context.Background(), msg, adapter); err == nil {
		t.Fatal("accepted excessive attachments")
	}
}
