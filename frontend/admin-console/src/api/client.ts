// Typed client for api-gateway's document-management endpoints. Mirrors
// docs/openapi/api-gateway.yaml / specs/implementation-plan.md §4.1.

const BASE_URL = import.meta.env.VITE_API_GATEWAY_URL ?? 'http://localhost:8080'

export interface DocumentSummary {
  id: string
  title: string
  category: string
  status: 'active' | 'deleted'
  current_version: number
}

export interface DocumentStatus {
  status: 'processing' | 'ready' | 'failed' | 'superseded'
  chunk_count: number
}

export interface IngestAccepted {
  document_id: string
  version: number
  status: string
}

export interface PathologyEvidence {
  chunk_id: string
  document_id: string
  page: number | null
  text?: string
}

// One row of GET /calls -- calls joined with patient identity and the current
// call_summaries snapshot (six signals, final triage, pathology validation). Mirrors
// docs/openapi/api-gateway.yaml's CallListItem / infra/postgres/migrations/
// 0003_final_triage.up.sql + 0004_pathology_validation.up.sql.
export interface CallListItem {
  id: string
  patient_id?: string
  patient_name?: string
  category?: string
  postop_day?: number
  status: 'active' | 'completed' | 'dropped'
  started_at: string
  ended_at?: string

  pain_nrs?: number
  fever_c?: number
  mobility?: 'normal' | 'limitada_esperada' | 'incapacitante_nueva'
  wound?: 'normal' | 'eritema_leve' | 'secrecion_purulenta'
  appetite?: 'normal' | 'levemente_disminuido' | 'muy_disminuido'
  sleep?: 'normal' | 'levemente_alterado' | 'muy_alterado'

  final_triage?: 'verde' | 'amarillo' | 'rojo'
  triage_rationale?: string
  triage_confidence?: number
  missing_info: string[]
  pathology_assessment?: string
  pathology_evidence: PathologyEvidence[]
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}/api/v1${path}`, init)
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${init?.method ?? 'GET'} ${path} failed: ${res.status} ${body}`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  listDocuments: () => request<DocumentSummary[]>('/documents'),

  uploadDocument: (file: File, title: string, category: string) => {
    const form = new FormData()
    form.append('file', file)
    form.append('title', title)
    form.append('category', category)
    return request<IngestAccepted>('/documents', { method: 'POST', body: form })
  },

  documentStatus: (id: string) => request<DocumentStatus>(`/documents/${id}/status`),

  deleteDocument: (id: string) => request<void>(`/documents/${id}`, { method: 'DELETE' }),

  listCalls: () => request<CallListItem[]>('/calls'),
}
