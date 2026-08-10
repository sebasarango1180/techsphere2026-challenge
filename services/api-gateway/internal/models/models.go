// Package models holds the DTOs api-gateway hands back over HTTP. Field shapes mirror
// docs/openapi/api-gateway.yaml and the Postgres schema in
// infra/postgres/migrations/0001_init.up.sql + 0002_patient_context.up.sql -- if one
// changes, update all three together.
package models

import "time"

// Patient. Category is the clean key (e.g. "cholecystitis") that decision.py's rule
// table and vector-store's category_hint key off of -- Procedure stays the
// human-readable Spanish name, matching the reference dataset's own split between
// `modulo_synthea` and `procedimiento` (docs/dataset-eda.md §4).
type Patient struct {
	ID            string    `json:"id"`
	ExternalRef   *string   `json:"external_ref,omitempty"`
	Name          string    `json:"name"`
	Procedure     *string   `json:"procedure,omitempty"`
	Category      *string   `json:"category,omitempty"`
	SurgeryDate   *string   `json:"surgery_date,omitempty"` // YYYY-MM-DD
	Age           *int      `json:"age,omitempty"`
	Gender        *string   `json:"gender,omitempty"`
	Comorbidities []string  `json:"comorbidities"`
	NationalID    *string   `json:"national_id,omitempty"`
	Address       *string   `json:"address,omitempty"`
	City          *string   `json:"city,omitempty"`
	Department    *string   `json:"department,omitempty"`
	EPS           *string   `json:"eps,omitempty"`
	CreatedAt     time.Time `json:"created_at"`
}

// PatientCreate: Name and Category are the two fields the rest of the system actually
// depends on (identification, and decision.py's rule gating / retrieval's category_hint
// respectively) -- everything else is nullable/optional context.
type PatientCreate struct {
	ExternalRef   string   `json:"external_ref"`
	Name          string   `json:"name" binding:"required"`
	Procedure     string   `json:"procedure"`
	Category      string   `json:"category" binding:"required"`
	SurgeryDate   string   `json:"surgery_date"` // YYYY-MM-DD
	Age           *int     `json:"age"`
	Gender        string   `json:"gender"`
	Comorbidities []string `json:"comorbidities"`
	NationalID    string   `json:"national_id"`
	Address       string   `json:"address"`
	City          string   `json:"city"`
	Department    string   `json:"department"`
	EPS           string   `json:"eps"`
}

type DocumentSummary struct {
	ID             string `json:"id"`
	Title          string `json:"title"`
	Category       string `json:"category"`
	Status         string `json:"status"` // active | deleted
	CurrentVersion int    `json:"current_version"`
}

type DocumentStatus struct {
	Status     string `json:"status"` // processing | ready | failed | superseded
	ChunkCount int    `json:"chunk_count"`
}

type IngestAccepted struct {
	DocumentID string `json:"document_id"`
	Version    int    `json:"version"`
	Status     string `json:"status"`
}

// CallCreateRequest: both fields optional -- an anonymous call (no patient_id) is still
// a valid call, it just won't have category-gated rules or room metadata to work with
// (voice-agent falls back to category=None, same as before this existed).
type CallCreateRequest struct {
	PatientID string `json:"patient_id"`
	PostopDay *int   `json:"postop_day"`
}

type CallCreated struct {
	CallID       string `json:"call_id"`
	LiveKitRoom  string `json:"livekit_room"`
	LiveKitToken string `json:"livekit_token"`
}

type Turn struct {
	ID                string    `json:"id"`
	Role              string    `json:"role"` // patient | agent | third_party
	Text              string    `json:"text"`
	STTMs             *int      `json:"stt_ms,omitempty"`
	RetrievalMs       *int      `json:"retrieval_ms,omitempty"`
	LLMMs             *int      `json:"llm_ms,omitempty"`
	TTSMs             *int      `json:"tts_ms,omitempty"`
	TokensIn          *int      `json:"tokens_in,omitempty"`
	TokensOut         *int      `json:"tokens_out,omitempty"`
	RetrievedChunkIDs []string  `json:"retrieved_chunk_ids"`
	CreatedAt         time.Time `json:"created_at"`
}

type Escalation struct {
	ID             string    `json:"id"`
	CallID         string    `json:"call_id"`
	Level          string    `json:"level"` // verde | amarillo | rojo
	Rationale      string    `json:"rationale"`
	TriggeredBy    string    `json:"triggered_by"` // model | rule | both
	CitedDocuments []any     `json:"cited_documents"`
	CreatedAt      time.Time `json:"created_at"`
}

// CallSummary is a LIVE clinical snapshot, not a write-once end-of-call artifact -- see
// infra/postgres/migrations/0002_patient_context.up.sql's comment on the table. The six
// structured fields mirror the reference dataset's own trajectory taxonomy exactly
// (docs/dataset-eda.md §3/§7) so a call's outcome is directly comparable to it.
type CallSummary struct {
	Procedure        string     `json:"procedure"`
	SymptomsReported string     `json:"symptoms_reported"`
	Decision         string     `json:"decision"`
	References       []any      `json:"references"`
	NextSteps        string     `json:"next_steps"`
	PainNRS          *int       `json:"pain_nrs,omitempty"`          // 0-10
	FeverC           *float64   `json:"fever_c,omitempty"`
	Mobility         *string    `json:"mobility,omitempty"`          // normal | limitada_esperada | incapacitante_nueva
	Wound            *string    `json:"wound,omitempty"`             // normal | eritema_leve | secrecion_purulenta
	Appetite         *string    `json:"appetite,omitempty"`          // normal | levemente_disminuido | muy_disminuido
	Sleep            *string    `json:"sleep,omitempty"`             // normal | levemente_alterado | muy_alterado
	UpdatedAt        *time.Time `json:"updated_at,omitempty"`
}

type CallDetail struct {
	ID         string       `json:"id"`
	PatientID  *string      `json:"patient_id,omitempty"`
	PostopDay  *int         `json:"postop_day,omitempty"`
	Status     string       `json:"status"` // active | completed | dropped
	Turns      []Turn       `json:"turns"`
	Escalation *Escalation  `json:"escalation,omitempty"`
	Summary    *CallSummary `json:"summary,omitempty"`
}

// MetricsSummary is what the README's required metrics table (specs/implementation-plan.md
// §0) gets computed from -- see internal/httpapi/metrics.go for the SQL that fills this in.
type MetricsSummary struct {
	P50Ms              float64 `json:"p50_ms"`
	P95Ms              float64 `json:"p95_ms"`
	TokensIn           int64   `json:"tokens_in"`
	TokensOut          int64   `json:"tokens_out"`
	RAGQueriesPerCall  float64 `json:"rag_queries_per_call"`
	EstCostPerCall     float64 `json:"est_cost_per_call"`
}
