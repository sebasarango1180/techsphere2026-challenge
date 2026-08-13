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

// CallCreateRequest: all fields optional -- an anonymous call (no patient_id) is still a
// valid call. PatientName/Age/Comorbidities are the ad-hoc equivalent for an anonymous
// caller who wants the agent to have SOME context without being a registered patient
// (call-interface's pre-call form) -- only used when PatientID is empty; a registered
// patient's own DB row remains the source of truth otherwise (see CreateCall).
type CallCreateRequest struct {
	PatientID     string   `json:"patient_id"`
	PostopDay     *int     `json:"postop_day"`
	PatientName   string   `json:"patient_name"`
	Age           *int     `json:"age"`
	Comorbidities []string `json:"comorbidities"`
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

// CallSummary combines the LIVE clinical snapshot (see
// infra/postgres/migrations/0002_patient_context.up.sql's comment on the table) with the
// end-of-call classification added by 0003_final_triage.up.sql and the KB-grounded
// pathology validation added by 0004_pathology_validation.up.sql. The six structured
// signal fields mirror the reference dataset's own trajectory taxonomy exactly
// (docs/dataset-eda.md §3/§7) so a call's outcome is directly comparable to it.
type CallSummary struct {
	Procedure        string     `json:"procedure"`
	SymptomsReported string     `json:"symptoms_reported"`
	Decision         string     `json:"decision"`
	References       []any      `json:"references"`
	NextSteps        string     `json:"next_steps"`
	PainNRS          *int       `json:"pain_nrs,omitempty"` // 0-10
	FeverC           *float64   `json:"fever_c,omitempty"`
	Mobility         *string    `json:"mobility,omitempty"` // normal | limitada_esperada | incapacitante_nueva
	Wound            *string    `json:"wound,omitempty"`    // normal | eritema_leve | secrecion_purulenta
	Appetite         *string    `json:"appetite,omitempty"` // normal | levemente_disminuido | muy_disminuido
	Sleep            *string    `json:"sleep,omitempty"`    // normal | levemente_alterado | muy_alterado
	UpdatedAt        *time.Time `json:"updated_at,omitempty"`

	// FinalTriage is the authoritative end-of-call classification (max of the model's
	// whole-transcript read and the deterministic rule layer's worst finding) -- distinct
	// from Escalation, which is the most recent REAL-TIME rule-layer hit during the call.
	FinalTriage         *string  `json:"final_triage,omitempty"` // verde | amarillo | rojo
	TriageRationale     *string  `json:"triage_rationale,omitempty"`
	TriageConfidence    *float64 `json:"triage_confidence,omitempty"`
	MissingInfo         []any    `json:"missing_info"`
	PathologyAssessment *string  `json:"pathology_assessment,omitempty"`
	PathologyEvidence   []any    `json:"pathology_evidence"`
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

// CallListItem is one row of GET /api/v1/calls -- calls joined with its patient identity
// and current call_summaries snapshot, so the admin console's "Calls" tab can render the
// whole list without an N+1 GetCallSummary round-trip per row (see
// infra/postgres/migrations/0003_final_triage.up.sql's comment on the intended UI).
type CallListItem struct {
	ID          string     `json:"id"`
	PatientID   *string    `json:"patient_id,omitempty"`
	PatientName *string    `json:"patient_name,omitempty"`
	Category    *string    `json:"category,omitempty"`
	PostopDay   *int       `json:"postop_day,omitempty"`
	Status      string     `json:"status"` // active | completed | dropped
	StartedAt   time.Time  `json:"started_at"`
	EndedAt     *time.Time `json:"ended_at,omitempty"`

	// Age/Comorbidities: from patients (registered patient) or calls' own ad-hoc columns
	// (anonymous call, call-interface's pre-call form) -- see infra/postgres/migrations/
	// 0005_anonymous_call_context.up.sql. PatientName above is already the same kind of
	// COALESCE.
	Age           *int  `json:"age,omitempty"`
	Comorbidities []any `json:"comorbidities"`

	PainNRS  *int     `json:"pain_nrs,omitempty"`
	FeverC   *float64 `json:"fever_c,omitempty"`
	Mobility *string  `json:"mobility,omitempty"`
	Wound    *string  `json:"wound,omitempty"`
	Appetite *string  `json:"appetite,omitempty"`
	Sleep    *string  `json:"sleep,omitempty"`

	FinalTriage         *string  `json:"final_triage,omitempty"`
	TriageRationale     *string  `json:"triage_rationale,omitempty"`
	TriageConfidence    *float64 `json:"triage_confidence,omitempty"`
	MissingInfo         []any    `json:"missing_info"`
	PathologyAssessment *string  `json:"pathology_assessment,omitempty"`
	PathologyEvidence   []any    `json:"pathology_evidence"`
}

// MetricsSummary is what the README's required metrics table (specs/implementation-plan.md
// §0) gets computed from -- see internal/httpapi/metrics.go for the SQL that fills this in.
// Both per-turn and per-call token averages are reported since the rubric asks for both
// (§5), not just one or the other.
type MetricsSummary struct {
	P50Ms             float64 `json:"p50_ms"`
	P95Ms             float64 `json:"p95_ms"`
	TokensInPerTurn   float64 `json:"tokens_in_per_turn"`
	TokensOutPerTurn  float64 `json:"tokens_out_per_turn"`
	TokensInPerCall   float64 `json:"tokens_in_per_call"`
	TokensOutPerCall  float64 `json:"tokens_out_per_call"`
	RAGQueriesPerCall float64 `json:"rag_queries_per_call"`
	EstCostPerCall    float64 `json:"est_cost_per_call"`
}
