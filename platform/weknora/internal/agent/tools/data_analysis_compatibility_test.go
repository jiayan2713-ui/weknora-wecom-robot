package tools

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestCompatibilityCSVPreservesOriginalAndRejectsStaleSource(t *testing.T) {
	dir := t.TempDir()
	original := filepath.Join(dir, "original.xls")
	csv := filepath.Join(dir, "table.csv")
	if err := os.WriteFile(original, []byte("immutable workbook"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(csv, []byte("A,__sheet_name\nvalue,Sheet1\n"), 0600); err != nil {
		t.Fatal(err)
	}
	sourceHash, _ := fileSHA256(original)
	csvHash, _ := fileSHA256(csv)
	manifestPath := filepath.Join(dir, "manifest.json")
	writeManifest := func(name string) {
		t.Helper()
		raw, err := json.Marshal(compatibilityManifest{Version: 1, Files: map[string]compatibilityTable{
			"selected": {SourceSHA256: sourceHash, CSVSHA256: csvHash, CSVFile: name},
		}})
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(manifestPath, raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	writeManifest("table.csv")
	got, ok, err := resolveCompatibilityCSV(manifestPath, "selected", original)
	if err != nil || !ok || got != csv {
		t.Fatalf("verified compatibility table: got=%q ok=%v err=%v", got, ok, err)
	}
	if hash, _ := fileSHA256(original); hash != sourceHash {
		t.Fatal("original changed")
	}
	if _, ok, err := resolveCompatibilityCSV(manifestPath, "other", original); ok || err != nil {
		t.Fatalf("unselected knowledge affected: %v %v", ok, err)
	}
	writeManifest("../table.csv")
	if _, _, err := resolveCompatibilityCSV(manifestPath, "selected", original); err == nil {
		t.Fatal("accepted path traversal")
	}
	writeManifest("table.csv")
	if err := os.WriteFile(csv, []byte("corrupted"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := resolveCompatibilityCSV(manifestPath, "selected", original); err == nil {
		t.Fatal("accepted modified CSV")
	}
	if err := os.WriteFile(original, []byte("new workbook version"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := resolveCompatibilityCSV(manifestPath, "selected", original); err == nil {
		t.Fatal("accepted changed source")
	}
}

func TestCompatibilityCSVIsOptional(t *testing.T) {
	if _, ok, err := resolveCompatibilityCSV("", "id", "unused"); ok || err != nil {
		t.Fatalf("disabled compatibility affected normal loading: %v %v", ok, err)
	}
}
