package service

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

func TestLocalExtractiveSummaryRequiresExactScopeAndModerationCode(t *testing.T) {
	content := "This original paragraph contains enough source text for a representative extract."
	moderationErr := errors.New("provider: DataInspectionFailed")
	tests := []struct {
		name, allowed, id string
		err               error
	}{
		{"disabled", "", "target", moderationErr},
		{"different knowledge", "target-other", "target", moderationErr},
		{"empty knowledge", ",", "", moderationErr},
		{"network failure", "target", "target", errors.New("connection reset")},
		{"rate limit", "target", "target", errors.New("429 rate limit")},
		{"generic refusal", "target", "target", errors.New("content not accepted")},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv(extractiveSummaryKnowledgeIDsEnv, test.allowed)
			got, err := localExtractiveSummary(test.id, content, test.err)
			if got != "" || err != test.err {
				t.Fatalf("expected unchanged model error, got summary %q, err %v", got, err)
			}
		})
	}
}

func TestLocalExtractiveSummaryLabelsMethodAndSamplesWholeSource(t *testing.T) {
	t.Setenv(extractiveSummaryKnowledgeIDsEnv, "other, target ")
	var source strings.Builder
	for i := 0; i < 20; i++ {
		fmt.Fprintf(&source, "# Original heading %02d\nOriginal passage %02d: this is verbatim source material that describes the document.\n", i, i)
	}
	for _, code := range []string{"data_inspection_failed", "DataInspectionFailed"} {
		got, err := localExtractiveSummary("target", source.String(), errors.New(code))
		if err != nil {
			t.Fatal(err)
		}
		for _, want := range []string{
			"【本地原文摘录摘要】", "未经生成模型改写", "摘录不覆盖全文",
			"# Original heading 00", "# Original heading 19",
			"Original passage 00:", "Original passage 10:", "Original passage 19:",
		} {
			if !strings.Contains(got, want) {
				t.Errorf("missing %q in summary %q", want, got)
			}
		}
		if count := strings.Count(got, "Original passage"); count != 8 {
			t.Errorf("got %d passages, want 8", count)
		}
	}
}

func TestLocalExtractiveSummaryRejectsUnusableSource(t *testing.T) {
	t.Setenv(extractiveSummaryKnowledgeIDsEnv, "target")
	for _, source := range []string{"", "# Heading only\n![image](https://example.test/a.png)", "[...content omitted...]"} {
		got, err := localExtractiveSummary("target", source, errors.New("data_inspection_failed"))
		if err == nil || got != "" {
			t.Fatalf("unusable source must fail, summary=%q err=%v", got, err)
		}
	}
}

func TestExtractiveSummaryPartsPreservesSourceAndDisclosesTruncation(t *testing.T) {
	longLine := strings.Repeat("原文", 180)
	paragraph := "Original paragraph contains enough text and must not be repeated in the output."
	_, parts := extractiveSummaryParts(paragraph + "\r\n" + paragraph + "\n" + longLine)
	if len(parts) != 3 || parts[0] != paragraph || parts[1] != string([]rune(longLine)[:260])+" […]" ||
		parts[2] != "[…] "+string([]rune(longLine)[260:]) {
		t.Fatalf("unexpected source excerpts: %#v", parts)
	}
}
