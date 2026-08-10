// Package vectorstore is the api-gateway-side client for the vector-store service
// (docs/openapi/vector-store.yaml). api-gateway owns document_id issuance (see
// specs/implementation-plan.md §2.4); this client is how it tells vector-store to
// actually index or drop the bytes.
package vectorstore

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"time"
)

type Client struct {
	baseURL string
	http    *http.Client
}

// No Client-level Timeout is set deliberately -- http.Client.Timeout is a hard wall-clock
// ceiling that overrides any *longer* context deadline a caller sets (found this the hard
// way: an earlier version set Timeout: 60s here, which silently clipped Ingest's own
// 5-minute context deadline below back down to 60s). Every call below sets its own
// context.WithTimeout instead, sized to what that specific operation actually needs.
func New(baseURL string) *Client {
	return &Client{
		baseURL: baseURL,
		http:    &http.Client{},
	}
}

const (
	// searchAndDeleteTimeout covers fast operations.
	searchAndDeleteTimeout = 15 * time.Second
	// ingestTimeout is generous -- found for real, not theoretical: OCR
	// (app/chunking.py's pytesseract fallback) on a multi-page scanned PDF, plus BGE-M3
	// embedding a full document's worth of chunks, can genuinely take a while. This is
	// separate from vector-store pre-warming its embedding model at startup (that fixes
	// the *one-time* model-load cost hitting whichever request arrives first; this
	// timeout is for the *inherent, per-document* processing time, which warming up
	// doesn't change).
	ingestTimeout = 5 * time.Minute
)

type IngestResult struct {
	ChunkCount int    `json:"chunk_count"`
	Status     string `json:"status"`
}

// Ingest uploads a document version for parsing/chunking/embedding.
// TODO(workstream A/B): stream large PDFs instead of buffering fully in memory once real
// files are involved; fine for scaffolding.
func (c *Client) Ingest(ctx context.Context, documentID string, version int, category string, filename string, file io.Reader) (IngestResult, error) {
	ctx, cancel := context.WithTimeout(ctx, ingestTimeout)
	defer cancel()

	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	_ = w.WriteField("document_id", documentID)
	_ = w.WriteField("version", fmt.Sprintf("%d", version))
	_ = w.WriteField("category", category)
	part, err := w.CreateFormFile("file", filename)
	if err != nil {
		return IngestResult{}, err
	}
	if _, err := io.Copy(part, file); err != nil {
		return IngestResult{}, err
	}
	if err := w.Close(); err != nil {
		return IngestResult{}, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/v1/ingest", &buf)
	if err != nil {
		return IngestResult{}, err
	}
	req.Header.Set("Content-Type", w.FormDataContentType())

	resp, err := c.http.Do(req)
	if err != nil {
		return IngestResult{}, err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		return IngestResult{}, fmt.Errorf("vector-store ingest failed: %s: %s", resp.Status, body)
	}

	var out IngestResult
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return IngestResult{}, err
	}
	return out, nil
}

// DeleteDocument soft-deletes all chunks for a document, synchronously -- see plan §2.4;
// G5 is tested live, so retrieval must exclude it before this call returns.
func (c *Client) DeleteDocument(ctx context.Context, documentID string) error {
	ctx, cancel := context.WithTimeout(ctx, searchAndDeleteTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, c.baseURL+"/v1/documents/"+documentID, nil)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 && resp.StatusCode != http.StatusNotFound {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("vector-store delete failed: %s: %s", resp.Status, body)
	}
	return nil
}
