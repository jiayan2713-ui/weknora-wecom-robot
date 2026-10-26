package service

import (
	"errors"
	"os"
	"strings"
	"unicode/utf8"
)

const extractiveSummaryKnowledgeIDsEnv = "WEKNORA_EXTRACTIVE_SUMMARY_KNOWLEDGE_IDS"

// localExtractiveSummary is a scoped, explicit alternative to generative
// summaries. It never sends rejected content to another model. Operators must
// enable each knowledge ID separately; other failures retain normal retry and
// failure handling. The method is persisted visibly in the summary itself.
func localExtractiveSummary(knowledgeID, content string, modelErr error) (string, error) {
	if modelErr == nil || knowledgeID == "" || !extractiveSummaryEnabled(knowledgeID) {
		return "", modelErr
	}
	code := strings.ToLower(modelErr.Error())
	if !strings.Contains(code, "data_inspection_failed") && !strings.Contains(code, "datainspectionfailed") {
		return "", modelErr
	}

	headings, paragraphs := extractiveSummaryParts(content)
	if len(paragraphs) == 0 {
		return "", errors.New("local extractive summary: no usable source passages after model content-inspection refusal")
	}
	var out strings.Builder
	out.WriteString("【本地原文摘录摘要】\n生成方式：摘要模型内容审核拒绝（data_inspection_failed）；以下为本地直接摘录的目录和代表性原文，未经生成模型改写。摘录不覆盖全文，事实以原文为准。\n")
	if len(headings) > 0 {
		out.WriteString("\n原文目录（节选）：\n")
		for _, heading := range evenlySpacedExcerpts(headings, 12) {
			out.WriteString(heading + "\n")
		}
	}
	out.WriteString("\n代表性原文（按原文顺序节选）：\n")
	for _, paragraph := range evenlySpacedExcerpts(paragraphs, 8) {
		out.WriteString("\n" + paragraph + "\n")
	}
	return strings.TrimSpace(out.String()), nil
}

func extractiveSummaryEnabled(knowledgeID string) bool {
	for _, allowed := range strings.Split(os.Getenv(extractiveSummaryKnowledgeIDsEnv), ",") {
		if strings.TrimSpace(allowed) == knowledgeID {
			return true
		}
	}
	return false
}

// Keep verbatim lines, rather than inventing claims or taking only the first
// 500 characters. Even spacing below samples the beginning, middle and end.
func extractiveSummaryParts(content string) ([]string, []string) {
	var headings, paragraphs []string
	seen := make(map[string]bool)
	for _, line := range strings.Split(strings.ReplaceAll(content, "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || seen[line] || strings.Contains(line, "[...content omitted...]") {
			continue
		}
		seen[line] = true
		n := utf8.RuneCountInString(line)
		if strings.HasPrefix(line, "#") && n <= 160 {
			headings = append(headings, line)
			continue
		}
		if n < 30 || strings.HasPrefix(line, "![") || strings.HasPrefix(line, "<img") {
			continue
		}
		// Split long source lines into candidates too, so a document stored
		// as one paragraph is sampled across its whole body. Explicit
		// ellipses disclose incomplete passages without changing their words.
		if n > 260 {
			runes := []rune(line)
			for start := 0; start < n; start += 260 {
				end := min(start+260, n)
				excerpt := string(runes[start:end])
				if start > 0 {
					excerpt = "[…] " + excerpt
				}
				if end < n {
					excerpt += " […]"
				}
				paragraphs = append(paragraphs, excerpt)
			}
			continue
		}
		paragraphs = append(paragraphs, line)
	}
	return headings, paragraphs
}

func evenlySpacedExcerpts(parts []string, limit int) []string {
	if len(parts) <= limit {
		return parts
	}
	selected := make([]string, limit)
	for i := range selected {
		selected[i] = parts[i*(len(parts)-1)/(limit-1)]
	}
	return selected
}
