package im

import (
	"context"
	"errors"
	"github.com/Tencent/WeKnora/internal/models/vlm"
	"github.com/Tencent/WeKnora/internal/types"
	"github.com/Tencent/WeKnora/internal/types/interfaces"
	"strings"
	"testing"
)

type imVisionModels struct {
	interfaces.ModelService
	model vlm.VLM
	err   error
}

func (m imVisionModels) GetVLMModel(context.Context, string) (vlm.VLM, error) { return m.model, m.err }

type imVisionModel struct {
	inputs []string
	fail   bool
}

func (m *imVisionModel) Predict(_ context.Context, data [][]byte, _ string) (string, error) {
	if m.fail {
		return "", errors.New("vision unavailable")
	}
	m.inputs = append(m.inputs, string(data[0]))
	return "图中编号：" + string(data[0]), nil
}
func (*imVisionModel) GetModelName() string { return "test-vision" }
func (*imVisionModel) GetModelID() string   { return "test-vision-id" }
func TestIMImageAnalysisPersistsEveryImageForHistory(t *testing.T) {
	vision := &imVisionModel{}
	svc := &Service{modelService: imVisionModels{model: vision}}
	agent := &types.CustomAgent{}
	agent.Config.VLMModelID = "configured-vision"
	attachments := types.MessageAttachments{{FileName: "a.png", FileType: ".png"}, {FileName: "b.png", FileType: ".png"}}
	downloads := []*imDownloadedAttachment{{content: []byte("ALPHA-4219")}, {content: []byte("BETA-7361")}}
	if err := svc.analyzeIMImages(context.Background(), agent, "读取编号", attachments, downloads); err != nil {
		t.Fatal(err)
	}
	payload := createIMUserMessagePayload("session", "读取编号", "request", attachments)
	if len(vision.inputs) != 2 || !strings.Contains(payload.Attachments.BuildPrompt(), "ALPHA-4219") || !strings.Contains(payload.Attachments.BuildPrompt(), "BETA-7361") {
		t.Fatal("lost image context before persistence")
	}
}
func TestIMImageAnalysisDoesNotPretendToRead(t *testing.T) {
	attachments := types.MessageAttachments{{FileType: ".png"}}
	downloads := []*imDownloadedAttachment{{content: []byte("image")}}
	if err := (&Service{}).analyzeIMImages(context.Background(), nil, "read", attachments, downloads); err == nil {
		t.Fatal("accepted image without readable content or vision model")
	}
	agent := &types.CustomAgent{}
	agent.Config.VLMModelID = "vision"
	svc := &Service{modelService: imVisionModels{model: &imVisionModel{fail: true}}}
	if err := svc.analyzeIMImages(context.Background(), agent, "read", attachments, downloads); err == nil {
		t.Fatal("accepted failed vision analysis")
	}
}

func TestIMImagePlaceholderIsNotReadableText(t *testing.T) {
	for _, content := range []string{"", "![photo](images/photo.png)", "<img src=\"photo.png\">"} {
		if hasReadableIMImageText(content) {
			t.Fatalf("image placeholder treated as OCR: %q", content)
		}
	}
	if !hasReadableIMImageText("![photo](images/photo.png)\n编号：REAL-2047") {
		t.Fatal("discarded real OCR")
	}
}
