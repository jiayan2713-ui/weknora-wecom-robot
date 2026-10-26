package agent

import (
	"context"
	"testing"

	"github.com/Tencent/WeKnora/internal/models/chat"
	"github.com/Tencent/WeKnora/internal/types"
	"github.com/stretchr/testify/require"
)

func TestFreshnessQuestionRouting(t *testing.T) {
	for _, q := range []string{"土耳其普通护照免签国家的数量", "葡萄牙移民政策", "加拿大申请条件", "目前申请费用是多少", "翻译政策并核实是否仍有效", "不要只看知识库，查土耳其免签数量"} {
		require.True(t, freshnessRequired(q, ""), q)
	}
	for _, q := range []string{"只看知识库，土耳其免签数量", "不要联网，解释政策", "翻译：土耳其签证政策", "解释历史版本的申请政策", "2024年土耳其免签国家数量", "对比2023年和2024年的政策", "公司报价中的申请费用", "你好"} {
		require.False(t, freshnessRequired(q, ""), q)
	}
	require.True(t, freshnessRequired("这个项目有什么要求", "资料口径不一致，无法确认时效"))
}

func freshnessTestState(success bool, rows []map[string]interface{}) *types.AgentState {
	return &types.AgentState{RoundSteps: []types.AgentStep{{ToolCalls: []types.ToolCall{{Name: "web_search", Result: &types.ToolResult{Success: success, Data: map[string]interface{}{"results": rows}}}}}}}
}

func TestFreshnessGateRequiresCurrentEvidenceAndOriginalLink(t *testing.T) {
	e := &AgentEngine{config: &types.AgentConfig{SystemPrompt: freshnessPolicyMarker, WebSearchEnabled: true}}
	q := "土耳其普通护照免签国家的数量"
	url := "https://example.com/original?q=a&b=2#part"
	state := freshnessTestState(true, []map[string]interface{}{{"url": url}})
	require.Equal(t, "missing_original_source_link", e.freshnessMissing(q, "134个 https://example.com/", state))
	require.Empty(t, e.freshnessMissing(q, "来源 "+url, state))
	require.Equal(t, "missing_current_web_evidence", e.freshnessMissing(q, "来源 "+url, freshnessTestState(false, []map[string]interface{}{{"url": url}})))
	require.Equal(t, "missing_current_web_evidence", e.freshnessMissing(q, "来源 "+url, freshnessTestState(true, nil)))
	require.Equal(t, "missing_current_web_evidence", e.freshnessMissing(q, "来源 "+url, &types.AgentState{}), "historical URL text cannot establish a current search")
	require.Empty(t, e.freshnessMissing("只看知识库，"+q, "旧资料134", nil))
	require.Empty(t, (&AgentEngine{config: &types.AgentConfig{}}).freshnessMissing(q, "旧资料134", nil), "other agents do not opt in")
}

func TestFreshnessCorrectionIsBoundedAndNeverReleasesUnverifiedCount(t *testing.T) {
	e := &AgentEngine{config: &types.AgentConfig{SystemPrompt: freshnessPolicyMarker, WebSearchEnabled: true}}
	messages := []chat.Message{}
	guards := &loopGuards{}
	for i := 0; i < 2; i++ {
		response := &types.ChatResponse{Content: "134个，采用2024年的清单", FinishReason: "stop"}
		require.True(t, e.enforceFreshness(context.Background(), "土耳其普通护照免签国家的数量", response, &types.AgentState{}, &messages, guards))
	}
	response := &types.ChatResponse{Content: "134个", FinishReason: "stop"}
	require.False(t, e.enforceFreshness(context.Background(), "土耳其普通护照免签国家的数量", response, &types.AgentState{}, &messages, guards))
	require.Equal(t, freshnessUnavailable, response.Content)
	require.NotContains(t, response.Content, "134")
	require.Len(t, messages, 2)
	e.config.WebSearchEnabled = false
	response.Content = "134个"
	require.False(t, e.enforceFreshness(context.Background(), "土耳其免签数量", response, nil, &messages, &loopGuards{}))
	require.Equal(t, freshnessUnavailable, response.Content)
}

func TestFreshnessHoldsStreamingCandidatesAndChecksFinalSynthesis(t *testing.T) {
	model := &mockChat{responses: []mockResponse{
		{chunks: []types.StreamResponse{{ResponseType: types.ResponseTypeAnswer, Content: "旧数据134个", Done: true, FinishReason: "stop"}}},
		{chunks: []types.StreamResponse{{ResponseType: types.ResponseTypeAnswer, Content: "旧数据134个", Done: true, FinishReason: "stop"}}},
	}}
	e := newTestEngine(t, model)
	e.config.SystemPrompt = freshnessPolicyMarker
	recorder := &truncationRecorder{}
	recorder.attach(e.eventBus)
	response, err := e.streamThinkingToEventBus(t.Context(), emptyMessages(), nil, 0, "session")
	require.NoError(t, err)
	require.False(t, response.AnswerStreamed)
	require.Empty(t, recorder.snapshot(), "candidate content must not reach the client before verification")
	state := &types.AgentState{}
	require.NoError(t, e.streamFinalAnswerToEventBus(t.Context(), "土耳其普通护照免签国家的数量", state, "session", emptyMessages()))
	require.Equal(t, freshnessUnavailable, state.FinalAnswer)
	for _, evt := range recorder.snapshot() {
		require.NotContains(t, evt.Content, "134")
	}
}

func TestFreshnessReviewRejectsMixedSecondaryNumbers(t *testing.T) {
	model := &mockChat{responses: []mockResponse{{chunks: []types.StreamResponse{{ResponseType: types.ResponseTypeAnswer, Content: `{"passed":false,"issue":"合计112与二手分项70+10+6不属于同一来源且不相等"}`, Done: true}}}}}
	e := newTestEngine(t, model)
	e.config.SystemPrompt = freshnessPolicyMarker
	state := freshnessTestState(true, []map[string]interface{}{{"url": "https://example.com/article", "snippet": "secondary claim 112"}})
	reason := e.reviewFreshnessEvidence(t.Context(), "土耳其普通护照免签国家的数量", "112，分项70+10+6 https://example.com/article", state)
	require.Contains(t, reason, "unsupported_current_claim")
	require.Empty(t, model.opts[0].Tools)
	require.Equal(t, "none", model.opts[0].ToolChoice)
}

func TestFreshnessReviewAllowsSupportedAnswerWithoutCorrection(t *testing.T) {
	model := &mockChat{responses: []mockResponse{{chunks: []types.StreamResponse{{ResponseType: types.ResponseTypeAnswer, Content: `{"passed":true,"issue":""}`, Done: true}}}}}
	e := newTestEngine(t, model)
	e.config.SystemPrompt = freshnessPolicyMarker
	e.config.WebSearchEnabled = true
	source := "https://authority.example/policy?version=1#rules"
	state := freshnessTestState(true, []map[string]interface{}{{"url": source, "snippet": "A current policy supported by this authority"}})
	response := &types.ChatResponse{Content: "根据主管机构原文，现行规则如下。来源：" + source, FinishReason: "stop"}
	messages := []chat.Message{}
	guards := &loopGuards{}
	require.False(t, e.enforceFreshness(t.Context(), "当前申请政策", response, state, &messages, guards))
	require.Equal(t, 0, guards.freshnessRetries)
	require.Empty(t, messages)
	require.Contains(t, response.Content, source)
}
