package httpapi

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"techsphere2026/api-gateway/internal/livekitauth"
	"techsphere2026/api-gateway/internal/models"
)

const tokenTTL = time.Hour

// roomMetadata is attached to the LiveKit room at creation time (internal/livekitadmin)
// and read back by voice-agent via ctx.room.metadata -- this is the piece that closes
// the "voice-agent has no way to know which patient/category a call is for" gap
// (specs/implementation-plan.md §2.1/§2.10). Field names are what voice-agent's
// _extract_call_context parses, so keep the two in sync.
type roomMetadata struct {
	PatientID   string `json:"patient_id,omitempty"`
	PatientName string `json:"patient_name,omitempty"`
	Category    string `json:"category,omitempty"`
	Procedure   string `json:"procedure,omitempty"`
	PostopDay   *int   `json:"postop_day,omitempty"`
}

// CreateCall implements POST /api/v1/calls: looks up the patient (if given), creates
// the LiveKit room WITH that patient's context as metadata, then mints a join token so
// the frontend never talks to LiveKit's admin API directly (plan §2.6). An anonymous
// call (no patient_id in the request body) is still valid -- it just leaves
// category-gated rules and the room metadata empty, same behavior as before patients
// existed.
func (s *Server) CreateCall(c *gin.Context) {
	ctx := c.Request.Context()

	var req models.CallCreateRequest
	_ = c.ShouldBindJSON(&req) // body is optional; a missing/empty body means an anonymous call

	callID := uuid.NewString()
	room := "call-" + callID

	var patientID *string
	meta := roomMetadata{PostopDay: req.PostopDay}
	if req.PatientID != "" {
		var name string
		var category, procedure *string
		err := s.DB.QueryRow(ctx, `SELECT name, category, procedure FROM patients WHERE id = $1`, req.PatientID).
			Scan(&name, &category, &procedure)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "unknown patient_id"})
			return
		}
		patientID = &req.PatientID
		meta.PatientID = req.PatientID
		meta.PatientName = name
		if category != nil {
			meta.Category = *category
		}
		if procedure != nil {
			meta.Procedure = *procedure
		}
	}

	metadataJSON, err := json.Marshal(meta)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	// Non-fatal on purpose: LiveKit auto-creates a room on first join even if this call
	// fails, just without metadata. G4 (live voice call working) is a hard gate -- a
	// LiveKit admin-API hiccup shouldn't take down basic call functionality, it should
	// just degrade to "voice-agent doesn't know the patient/category for this call",
	// which is the same behavior as before this feature existed.
	if err := s.LiveKit.CreateRoom(ctx, room, string(metadataJSON)); err != nil {
		slog.Error("livekit CreateRoom failed, continuing without room metadata", "room", room, "error", err)
	}

	token, err := livekitauth.MintToken(s.Cfg.LiveKitAPIKey, s.Cfg.LiveKitAPISecret, room, "patient-"+callID, tokenTTL)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	if _, err := s.DB.Exec(ctx, `
		INSERT INTO calls (id, patient_id, postop_day, status, stt_mode, llm_model, livekit_room)
		VALUES ($1, $2, $3, 'active', $4, $5, $6)
	`, callID, patientID, req.PostopDay, s.Cfg.STTMode, s.Cfg.LLMModel, room); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusCreated, models.CallCreated{
		CallID:       callID,
		LiveKitRoom:  room,
		LiveKitToken: token,
	})
}

// GetCall implements GET /api/v1/calls/:id.
func (s *Server) GetCall(c *gin.Context) {
	id := c.Param("id")
	ctx := c.Request.Context()

	var detail models.CallDetail
	detail.ID = id
	if err := s.DB.QueryRow(ctx, `SELECT patient_id, postop_day, status FROM calls WHERE id = $1`, id).
		Scan(&detail.PatientID, &detail.PostopDay, &detail.Status); err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "call not found"})
		return
	}

	rows, err := s.DB.Query(ctx, `
		SELECT id, role, text, stt_ms, retrieval_ms, llm_ms, tts_ms, tokens_in, tokens_out,
		       retrieved_chunk_ids, created_at
		FROM turns WHERE call_id = $1 ORDER BY created_at ASC
	`, id)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	defer rows.Close()
	for rows.Next() {
		var t models.Turn
		if err := rows.Scan(&t.ID, &t.Role, &t.Text, &t.STTMs, &t.RetrievalMs, &t.LLMMs, &t.TTSMs,
			&t.TokensIn, &t.TokensOut, &t.RetrievedChunkIDs, &t.CreatedAt); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		detail.Turns = append(detail.Turns, t)
	}

	// Most recent escalation only -- a call can have several as symptoms evolve;
	// TODO(workstream A): decide whether the console should show the full history
	// instead of just the latest once that UI exists.
	var esc models.Escalation
	err = s.DB.QueryRow(ctx, `
		SELECT id, call_id, level, rationale, triggered_by, cited_documents, created_at
		FROM escalations WHERE call_id = $1 ORDER BY created_at DESC LIMIT 1
	`, id).Scan(&esc.ID, &esc.CallID, &esc.Level, &esc.Rationale, &esc.TriggeredBy, &esc.CitedDocuments, &esc.CreatedAt)
	if err == nil {
		detail.Escalation = &esc
	} else if err != pgx.ErrNoRows {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	summary, err := fetchCallSummary(ctx, s.DB, id)
	if err == nil {
		detail.Summary = summary
	} else if err != pgx.ErrNoRows {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.JSON(http.StatusOK, detail)
}

// GetCallSummary implements GET /api/v1/calls/:id/summary.
func (s *Server) GetCallSummary(c *gin.Context) {
	id := c.Param("id")
	summary, err := fetchCallSummary(c.Request.Context(), s.DB, id)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "summary not available yet"})
		return
	}
	c.JSON(http.StatusOK, summary)
}

func fetchCallSummary(ctx context.Context, db *pgxpool.Pool, callID string) (*models.CallSummary, error) {
	var summary models.CallSummary
	err := db.QueryRow(ctx, `
		SELECT procedure, symptoms_reported, decision, "references", next_steps,
		       pain_nrs, fever_c, mobility, wound, appetite, sleep, updated_at
		FROM call_summaries WHERE call_id = $1
	`, callID).Scan(
		&summary.Procedure, &summary.SymptomsReported, &summary.Decision, &summary.References, &summary.NextSteps,
		&summary.PainNRS, &summary.FeverC, &summary.Mobility, &summary.Wound, &summary.Appetite, &summary.Sleep,
		&summary.UpdatedAt,
	)
	if err != nil {
		return nil, err
	}
	return &summary, nil
}
