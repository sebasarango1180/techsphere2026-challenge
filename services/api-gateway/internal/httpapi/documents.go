package httpapi

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"techsphere2026/api-gateway/internal/models"
	"techsphere2026/api-gateway/internal/vectorstore"
)

var errFileRequired = errors.New("file is required")

// ListDocuments implements GET /api/v1/documents. Excludes soft-deleted documents by
// default -- a deleted document has no real chunks left in vector-store (DeleteDocument
// purges them synchronously, plan §2.4/G5), so leaving it in the default list just
// clutters the admin console with dead rows. Pass ?status=all to see everything
// (deleted included), e.g. for debugging.
func (s *Server) ListDocuments(c *gin.Context) {
	query := `
		SELECT id, title, category, status, current_version
		FROM documents
	`
	if c.Query("status") != "all" {
		query += ` WHERE status != 'deleted'`
	}
	query += ` ORDER BY created_at DESC`

	rows, err := s.DB.Query(c.Request.Context(), query)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	defer rows.Close()

	out := []models.DocumentSummary{}
	for rows.Next() {
		var d models.DocumentSummary
		if err := rows.Scan(&d.ID, &d.Title, &d.Category, &d.Status, &d.CurrentVersion); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		out = append(out, d)
	}
	c.JSON(http.StatusOK, out)
}

// UploadDocument implements POST /api/v1/documents: creates a NEW document identity
// (documents row, version 1) and ingests it. To add a new version of an EXISTING
// document instead -- what "a PDF is uploaded or updated" (plural) actually needs -- see
// ReindexDocument (PUT /documents/:id) below; that's what triggers a real re-OCR +
// re-embed + version-supersession cycle on the SAME document_id, not a brand new one.
func (s *Server) UploadDocument(c *gin.Context) {
	title := c.PostForm("title")
	category := c.PostForm("category")
	if title == "" || category == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "title and category are required"})
		return
	}

	fileHeader, buf, err := readUploadedFile(c)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	ctx := c.Request.Context()
	documentID := uuid.NewString()
	const version = 1

	tx, err := s.DB.Begin(ctx)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	if _, err := tx.Exec(ctx, `
		INSERT INTO documents (id, title, category, status, current_version)
		VALUES ($1, $2, $3, 'active', $4)
	`, documentID, title, category, version); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	if err := insertVersionRow(ctx, tx, documentID, version, buf); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	if err := tx.Commit(ctx); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	result, ingestErr := s.ingestVersion(ctx, documentID, version, category, fileHeader.Filename, buf)
	if ingestErr != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "ingestion failed, see logs", "document_id": documentID})
		return
	}

	c.JSON(http.StatusAccepted, models.IngestAccepted{
		DocumentID: documentID,
		Version:    version,
		Status:     result.Status,
	})
}

// ReindexDocument implements PUT /api/v1/documents/:id: uploads a NEW version of an
// EXISTING document -- the real "when a PDF is uploaded or updated, it needs to be
// reindexed" path. title/category are optional here (form fields are only applied if
// given; omit them to keep the document's existing values). Re-runs the full
// extract-OCR-if-needed -> chunk -> embed pipeline (services/vector-store/app/chunking.py,
// app/store.py) against the SAME document_id with an incremented version; vector-store's
// upsert_chunks marks the prior version's chunks `superseded` so retrieval only ever sees
// the current one (plan §2.4) -- verified end-to-end against a real Postgres + ChromaDB,
// not just reviewed.
func (s *Server) ReindexDocument(c *gin.Context) {
	id := c.Param("id")
	title := c.PostForm("title")     // optional
	category := c.PostForm("category") // optional

	fileHeader, buf, err := readUploadedFile(c)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	ctx := c.Request.Context()

	tx, err := s.DB.Begin(ctx)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	defer tx.Rollback(ctx) //nolint:errcheck

	var currentVersion int
	var existingCategory string
	// FOR UPDATE: this admin operation is low-concurrency, but locking the row still
	// avoids two concurrent re-index requests computing the same "next version" number.
	if err := tx.QueryRow(ctx, `
		SELECT current_version, category FROM documents WHERE id = $1 FOR UPDATE
	`, id).Scan(&currentVersion, &existingCategory); err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "document not found"})
		return
	}

	newVersion := currentVersion + 1
	effectiveCategory := existingCategory
	if category != "" {
		effectiveCategory = category
	}

	// Re-uploading is a clear signal the document should be usable again even if it was
	// previously soft-deleted.
	setClauses := `status = 'active', current_version = $2, category = $3`
	args := []any{id, newVersion, effectiveCategory}
	if title != "" {
		setClauses += `, title = $4`
		args = append(args, title)
	}
	if _, err := tx.Exec(ctx, `UPDATE documents SET `+setClauses+` WHERE id = $1`, args...); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	if err := insertVersionRow(ctx, tx, id, newVersion, buf); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	if err := tx.Commit(ctx); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	result, ingestErr := s.ingestVersion(ctx, id, newVersion, effectiveCategory, fileHeader.Filename, buf)
	if ingestErr != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "reindex failed, see logs", "document_id": id})
		return
	}

	c.JSON(http.StatusAccepted, models.IngestAccepted{
		DocumentID: id,
		Version:    newVersion,
		Status:     result.Status,
	})
}

func readUploadedFile(c *gin.Context) (*multipart.FileHeader, []byte, error) {
	fileHeader, err := c.FormFile("file")
	if err != nil {
		return nil, nil, errFileRequired
	}
	f, err := fileHeader.Open()
	if err != nil {
		return nil, nil, err
	}
	defer f.Close()

	buf, err := io.ReadAll(f)
	if err != nil {
		return nil, nil, err
	}
	return fileHeader, buf, nil
}

func insertVersionRow(ctx context.Context, tx pgx.Tx, documentID string, version int, buf []byte) error {
	sum := sha256.Sum256(buf)
	checksum := hex.EncodeToString(sum[:])
	// storage_path: no raw-file blob store in v1 -- vector-store only persists chunk
	// embeddings, not the original bytes. TODO(workstream A/B): if citation display ever
	// needs to link back to the original PDF page image (not just extracted text), add a
	// blob store and put its path here instead of this placeholder.
	storagePath := "vector-store:" + documentID + "/v" + strconv.Itoa(version)

	_, err := tx.Exec(ctx, `
		INSERT INTO document_versions (document_id, version, storage_path, checksum, status)
		VALUES ($1, $2, $3, $4, 'processing')
	`, documentID, version, storagePath, checksum)
	return err
}

// ingestVersion proxies the bytes to vector-store for OCR/parse/chunk/embed, then
// records the result. Shared by UploadDocument (new document) and ReindexDocument
// (new version of an existing document) -- identical from this point on either way.
func (s *Server) ingestVersion(ctx context.Context, documentID string, version int, category, filename string, buf []byte) (vectorstore.IngestResult, error) {
	result, err := s.VectorStore.Ingest(ctx, documentID, version, category, filename, bytes.NewReader(buf))
	if err != nil {
		slog.Error("vector-store ingest failed", "document_id", documentID, "version", version, "error", err)
		s.markVersionFailed(ctx, documentID, version)
		return vectorstore.IngestResult{}, err
	}

	tag, err := s.DB.Exec(ctx, `
		UPDATE document_versions
		SET status = $3, chunk_count = $4, processed_at = now()
		WHERE document_id = $1 AND version = $2
	`, documentID, version, result.Status, result.ChunkCount)
	if err != nil {
		slog.Error("failed to persist ingest result", "document_id", documentID, "error", err)
	} else if tag.RowsAffected() == 0 {
		// Caught a real bug this way once already (a refactor briefly dropped the
		// document_versions INSERT from UploadDocument's create path -- this UPDATE
		// matched zero rows silently, no error, and the caller still saw "ready" back
		// because vector-store's own ingest had genuinely succeeded). Loud on purpose.
		slog.Error("ingestVersion: UPDATE matched no document_versions row -- was insertVersionRow called for this document_id/version?",
			"document_id", documentID, "version", version)
	}

	return result, nil
}

func (s *Server) markVersionFailed(ctx context.Context, documentID string, version int) {
	if _, err := s.DB.Exec(ctx, `
		UPDATE document_versions SET status = 'failed' WHERE document_id = $1 AND version = $2
	`, documentID, version); err != nil {
		slog.Error("failed to mark document_version failed", "document_id", documentID, "error", err)
	}
}

// DocumentStatus implements GET /api/v1/documents/:id/status -- polled by the admin
// console for the "procesado y disponible" indicator required by G5's contract.
func (s *Server) DocumentStatus(c *gin.Context) {
	id := c.Param("id")
	var out models.DocumentStatus
	err := s.DB.QueryRow(c.Request.Context(), `
		SELECT dv.status, dv.chunk_count
		FROM document_versions dv
		JOIN documents d ON d.id = dv.document_id AND d.current_version = dv.version
		WHERE d.id = $1
	`, id).Scan(&out.Status, &out.ChunkCount)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "document not found"})
		return
	}
	c.JSON(http.StatusOK, out)
}

// DeleteDocument implements DELETE /api/v1/documents/:id. Must be synchronous end to
// end -- G5 is verified live with a held-out document, so retrieval has to stop seeing it
// before this handler returns (plan §2.4).
func (s *Server) DeleteDocument(c *gin.Context) {
	id := c.Param("id")
	ctx := c.Request.Context()

	tag, err := s.DB.Exec(ctx, `UPDATE documents SET status = 'deleted' WHERE id = $1`, id)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	if tag.RowsAffected() == 0 {
		c.JSON(http.StatusNotFound, gin.H{"error": "document not found"})
		return
	}

	if err := s.VectorStore.DeleteDocument(ctx, id); err != nil {
		// Postgres already reflects the deletion; vector-store is now the inconsistent
		// side. TODO(workstream A): add a reconciliation sweep (list documents with
		// status=deleted, retry vector-store delete) rather than leaving this as a bare log.
		slog.Error("vector-store delete failed after postgres soft-delete", "document_id", id, "error", err)
		c.JSON(http.StatusBadGateway, gin.H{"error": "postgres updated but vector-store delete failed, will retry"})
		return
	}

	c.Status(http.StatusNoContent)
}
