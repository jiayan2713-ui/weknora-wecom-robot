package wecom

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"github.com/Tencent/WeKnora/internal/im"
	"testing"
)

func TestDecryptMediaEncodingsAndPadding(t *testing.T) {
	key := bytes.Repeat([]byte{54}, 32)
	for _, size := range []int{16, 32} {
		for n := 1; n <= 64; n++ {
			content := bytes.Repeat([]byte{'A'}, n)
			pad := size - n%size
			plain := append(append([]byte{}, content...), bytes.Repeat([]byte{byte(pad)}, pad)...)
			block, _ := aes.NewCipher(key)
			encrypted := make([]byte, len(plain))
			cipher.NewCBCEncrypter(block, key[:16]).CryptBlocks(encrypted, plain)
			for _, encoded := range []string{base64.StdEncoding.EncodeToString(key), base64.RawStdEncoding.EncodeToString(key)} {
				got, err := decryptAESCBC(encrypted, encoded)
				if err != nil || !bytes.Equal(got, content) {
					t.Fatalf("block=%d len=%d keylen=%d: %q %v", size, n, len(encoded), got, err)
				}
			}
		}
	}
}
func TestDecryptMediaRejectsCorruption(t *testing.T) {
	key := base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{54}, 32))
	for _, data := range [][]byte{nil, []byte("short"), bytes.Repeat([]byte{0}, 32)} {
		if _, err := decryptAESCBC(data, key); err == nil {
			t.Fatal("accepted corrupt ciphertext")
		}
	}
	if _, err := decryptAESCBC(bytes.Repeat([]byte{0}, 32), "bad key"); err == nil {
		t.Fatal("accepted invalid key")
	}
}
func TestMixedMessagePreservesCaptionAndEveryImage(t *testing.T) {
	msg := &botMessage{MsgID: "mixed-test"}
	msg.From.UserID = "tester"
	msg.Mixed.MsgItem = []botMixedItem{{MsgType: "text"}, {MsgType: "image"}, {MsgType: "image"}}
	msg.Mixed.MsgItem[0].Text.Content = "read both pictures"
	msg.Mixed.MsgItem[1].Image.URL = "https://example.com/a"
	msg.Mixed.MsgItem[1].Image.AESKey = "key-a"
	msg.Mixed.MsgItem[2].Image.URL = "https://example.com/b"
	msg.Mixed.MsgItem[2].Image.AESKey = "key-b"
	got := (&LongConnClient{}).convertMixedMessage(msg, "", im.ChatTypeDirect, "req-1")
	if got.Content != "read both pictures" || got.MessageType != im.MessageTypeImage || len(got.Attachments) != 2 {
		t.Fatalf("lost mixed content: %+v", got)
	}
	for i, want := range []string{"a", "b"} {
		if got.Attachments[i].FileKey != "https://example.com/"+want || got.Attachments[i].Extra["aes_key"] != "key-"+want {
			t.Fatal("lost independent keys")
		}
	}
	msg.Mixed.MsgItem = msg.Mixed.MsgItem[1:]
	got = (&LongConnClient{}).convertMixedMessage(msg, "", im.ChatTypeDirect, "req-1")
	if got.Content != "" || len(got.Attachments) != 2 {
		t.Fatal("lost image-only mixed resources")
	}
	msg.Mixed.MsgItem = nil
	if (&LongConnClient{}).convertMixedMessage(msg, "", im.ChatTypeDirect, "req-1") != nil {
		t.Fatal("accepted empty mixed message")
	}
}
