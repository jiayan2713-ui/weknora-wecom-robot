package im

import (
	"archive/zip"
	"bytes"
	"fmt"
	"net/http"
	"path/filepath"
	"strings"
)

// resolveIMAttachmentFileName infers missing or synthetic suffixes from decrypted bytes.
// WeCom download headers may describe encrypted octet-stream data rather than the original file.
func resolveIMAttachmentFileName(name string, content []byte) (string, error) {
	ext := strings.ToLower(filepath.Ext(name))
	detected := ""
	switch http.DetectContentType(content) {
	case "image/png":
		detected = ".png"
	case "image/jpeg":
		detected = ".jpg"
	case "image/gif":
		detected = ".gif"
	case "image/webp":
		detected = ".webp"
	case "application/pdf":
		detected = ".pdf"
	default:
		if bytes.HasPrefix(content, []byte("PK\x03\x04")) {
			archive, err := zip.NewReader(bytes.NewReader(content), int64(len(content)))
			if err != nil {
				return "", fmt.Errorf("invalid ZIP attachment: %w", err)
			}
			for _, file := range archive.File {
				switch file.Name {
				case "word/document.xml":
					detected = ".docx"
				case "xl/workbook.xml":
					detected = ".xlsx"
				case "ppt/presentation.xml":
					detected = ".pptx"
				}
			}
		} else if (ext == "" || ext == ".bin") && strings.HasPrefix(http.DetectContentType(content), "text/plain") {
			detected = ".txt"
		}
	}
	if name == "" {
		name = "attachment"
	}
	if detected != "" {
		if ext == "" || ext == ".bin" || ext == ".png" || ext == ".jpg" || ext == ".jpeg" {
			return strings.TrimSuffix(name, filepath.Ext(name)) + detected, nil
		}
	}
	if ext == "" {
		return "", fmt.Errorf("cannot determine attachment type from decrypted content")
	}
	return name, nil
}
