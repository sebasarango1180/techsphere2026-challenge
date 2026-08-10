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
}
