package tools

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// compatibilityManifest describes private cached-value tables derived from
// otherwise unreadable workbooks. Originals remain the authoritative blobs.
type compatibilityManifest struct {
	Version int                           `json:"version"`
	Files   map[string]compatibilityTable `json:"files"`
}

type compatibilityTable struct {
	SourceSHA256 string `json:"source_sha256"`
	CSVSHA256    string `json:"csv_sha256"`
	CSVFile      string `json:"csv_file"`
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// resolveCompatibilityCSV only accepts a manifest entry when both the current
// original bytes and derived CSV match their recorded hashes. A changed source
// must be regenerated rather than silently reading stale table data.
func resolveCompatibilityCSV(manifestPath, knowledgeID, originalPath string) (string, bool, error) {
	if manifestPath == "" {
		return "", false, nil
	}
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		return "", false, fmt.Errorf("read table compatibility manifest: %w", err)
	}
	var manifest compatibilityManifest
	if err := json.Unmarshal(raw, &manifest); err != nil || manifest.Version != 1 {
		return "", false, fmt.Errorf("invalid table compatibility manifest")
	}
	entry, ok := manifest.Files[knowledgeID]
	if !ok {
		return "", false, nil
	}
	if entry.CSVFile == "" || filepath.Base(entry.CSVFile) != entry.CSVFile || filepath.Ext(entry.CSVFile) != ".csv" {
		return "", false, fmt.Errorf("invalid table compatibility filename")
	}
	sourceHash, err := fileSHA256(originalPath)
	if err != nil || len(entry.SourceSHA256) != 64 || sourceHash != entry.SourceSHA256 {
		return "", false, fmt.Errorf("table compatibility source fingerprint mismatch; regenerate the derived table")
	}
	csvPath := filepath.Join(filepath.Dir(manifestPath), entry.CSVFile)
	info, err := os.Lstat(csvPath)
	if err != nil || !info.Mode().IsRegular() {
		return "", false, fmt.Errorf("table compatibility CSV is not a regular file")
	}
	csvHash, err := fileSHA256(csvPath)
	if err != nil || len(entry.CSVSHA256) != 64 || csvHash != entry.CSVSHA256 {
		return "", false, fmt.Errorf("table compatibility CSV fingerprint mismatch")
	}
	return csvPath, true, nil
}
