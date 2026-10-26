package modelcontext

import (
	"strings"
	"testing"

	"github.com/Tencent/WeKnora/internal/models/chat"
	"github.com/Tencent/WeKnora/internal/types"
	"github.com/stretchr/testify/require"
)

func TestOriginalWebURLSurvivesToolCompactionAndStreaming(t *testing.T) {
	urls := []string{
		"https://www.mfa.gov.cn/web/wjb_673085/zzjg_673183/lss_674689/",
		"https://example.com/路径/(item)?q=a%2Fb&lang=zh#section",
		"https://example.com/i1/page?id=i1&resource=res://9999#i1",
	}
	for _, citations := range []bool{true, false} {
		for _, rawURL := range urls {
			r := NewRegistry(citations)
			r.issues.Register("issue-real")
			result := &types.ToolResult{Success: true, Data: map[string]interface{}{
				"display_type": "web_search_results", "results": []map[string]interface{}{{"title": "Page", "url": rawURL, "snippet": "evidence"}},
			}}
			output := r.ModelToolResultForTool("web_search", result)
			require.Contains(t, output, `<url_ref id="w1"/>`)
			messages := r.EncodeMessages([]chat.Message{{Role: "tool", Name: "web_search", Content: output}})
			require.Contains(t, messages[0].Content, `<url_ref id="w1"/>`)
			input := `URL: <url_ref id="w1"/>`
			want := "URL: " + rawURL
			require.Equal(t, want, r.DecodeOutputText(input))
			for split := 0; split <= len(input); split++ {
				decoder := r.StreamDecoder()
				got := decoder.Feed(input[:split]) + decoder.Feed(input[split:]) + decoder.Flush()
				require.Equal(t, want, got, "split at %d", split)
			}
		}
	}
}

func TestOriginalWebURLRejectsUnknownAndHistoryOnlySources(t *testing.T) {
	r := NewRegistry(false)
	r.RegisterDocument("doc-real")
	r.EncodeMessages([]chat.Message{{Role: "assistant", Content: `<web url="https://example.com/history" title="Old"/>`}})
	for _, input := range []string{`<url_ref id="w99"/>`, `<url_ref id="w1"/>`, `<url_ref id="d1"/>`, `<url_ref id="w1" url="https://evil.example"/>`} {
		require.Empty(t, r.DecodeOutputText(input))
	}
	decoder := r.StreamDecoder()
	require.Empty(t, decoder.Feed(`<url_ref id="w`)+decoder.Flush())
	require.Contains(t, r.ProtocolPrompt(), "Never reconstruct, shorten, autocomplete or guess")
	require.False(t, strings.Contains(r.DecodeOutputText(`<url_ref id="w99"/>`), "url_ref"))
}
