import { useCallback, useEffect, useState } from 'react'
import { api, type CallListItem, type DocumentSummary } from './api/client'
import './App.css'

// Standalone admin app (ParticipantArtifacts/README.md's "consola de administracion"),
// separate from the patient-facing call-interface app -- see ../call-interface. Required
// minimum contract: upload / list / delete / "processed and available" indicator. G5 is
// verified live with a document outside the given corpus, so the upload -> processing ->
// ready loop here has to actually work, not just look right.
//
// No auth: deliberate scope decision, matching the challenge's explicit exclusion of
// "autenticacion empresarial o gestion de roles" (see root README.md).
//
// Two tabs: Documentos (knowledge base management, the original app) and Llamadas (the
// per-call final classification -- six signals, triage, pathology validation -- see
// infra/postgres/migrations/0003_final_triage.up.sql / 0004_pathology_validation.up.sql's
// comments on this being the intended UI for that data). No router library in this
// project (see package.json) -- a plain local tab switch is all this needs.
type Tab = 'documents' | 'calls'

export default function App() {
  const [tab, setTab] = useState<Tab>('documents')

  return (
    <div className="app-main">
      <h1>Consola de administracion</h1>
      <div className="tab-bar">
        <button
          type="button"
          className={`tab-button ${tab === 'documents' ? 'active' : ''}`}
          onClick={() => setTab('documents')}
        >
          Documentos
        </button>
        <button
          type="button"
          className={`tab-button ${tab === 'calls' ? 'active' : ''}`}
          onClick={() => setTab('calls')}
        >
          Llamadas
        </button>
      </div>

      {tab === 'documents' ? <DocumentsTab /> : <CallsTab />}
    </div>
  )
}

const CATEGORY_SUGGESTIONS = [
  'cholecystitis',
  'colorectal cancer',
  'appendicitis',
  'breast_cancer',
  'total joint replacement',
]

const POLL_INTERVAL_MS = 3000

function DocumentsTab() {
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [error, setError] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  const [category, setCategory] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const docs = await api.listDocuments()
      setDocuments(docs)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    refresh()
    // Poll while anything is still processing -- this is what drives the "procesado y
    // disponible" indicator without the user having to manually refresh.
    const id = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [refresh])

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault()
    if (!file || !title || !category) return
    setUploading(true)
    setError(null)
    try {
      await api.uploadDocument(file, title, category)
      setTitle('')
      setCategory('')
      setFile(null)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setUploading(false)
    }
  }

  async function handleDelete(id: string) {
    setError(null)
    try {
      await api.deleteDocument(id)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <>
      <p className="subtitle">Sube, revisa y elimina documentos de la base de conocimiento clinico.</p>

      {error && <div className="error-banner">{error}</div>}

      <section className="card">
        <h2 style={{ marginTop: 0 }}>Subir documento</h2>
        <form className="upload-form" onSubmit={handleUpload}>
          <input
            type="text"
            placeholder="Titulo del documento"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
          />
          <input
            type="text"
            list="category-suggestions"
            placeholder="Categoria (ej. appendicitis)"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            required
          />
          <datalist id="category-suggestions">
            {CATEGORY_SUGGESTIONS.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
          <input
            type="file"
            accept="application/pdf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            required
          />
          <button type="submit" className="big-button" disabled={uploading}>
            {uploading ? 'Subiendo...' : 'Subir documento'}
          </button>
        </form>
      </section>

      <section className="card">
        <h2 style={{ marginTop: 0 }}>Documentos cargados</h2>
        {documents.length === 0 ? (
          <p className="subtitle">Aun no hay documentos cargados.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Titulo</th>
                <th>Categoria</th>
                <th>Estado</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <DocumentRow key={doc.id} doc={doc} onDelete={() => handleDelete(doc.id)} />
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  )
}

function DocumentRow({ doc, onDelete }: { doc: DocumentSummary; onDelete: () => void }) {
  // Per-document status (processing/ready/failed) comes from a separate endpoint --
  // TODO(workstream D): batch this into the list response instead of one request per row
  // once document counts grow past a handful; fine for the corpus size in this challenge.
  const [status, setStatus] = useState<string>(doc.status === 'deleted' ? 'deleted' : 'processing')

  useEffect(() => {
    if (doc.status === 'deleted') return
    let cancelled = false
    api
      .documentStatus(doc.id)
      .then((s) => {
        if (!cancelled) setStatus(s.status)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [doc.id, doc.status])

  return (
    <tr>
      <td>{doc.title}</td>
      <td>{doc.category}</td>
      <td>
        <span className={`status-pill ${status}`}>{status}</span>
      </td>
      <td>
        <button
          type="button"
          className="big-button danger"
          style={{ padding: '10px 18px', fontSize: 15, minWidth: 0 }}
          onClick={onDelete}
        >
          Eliminar
        </button>
      </td>
    </tr>
  )
}

const SIGNAL_LABELS_ES: Record<string, string> = {
  pain_nrs: 'Dolor',
  fever_c: 'Fiebre',
  mobility: 'Movilidad',
  wound: 'Herida',
  appetite: 'Apetito',
  sleep: 'Sueno',
}

function CallsTab() {
  const [calls, setCalls] = useState<CallListItem[]>([])
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setCalls(await api.listCalls())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    refresh()
    // A call in progress gets its final classification only at hangup (summarize_call
    // runs once, at call end -- see services/voice-agent/app/main.py) -- polling is what
    // turns an "active" row into its final triage/pathology without a manual refresh.
    const id = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [refresh])

  return (
    <>
      <p className="subtitle">
        Clasificacion final de cada llamada de seguimiento: senales clinicas, triage y validacion contra la base de
        conocimiento.
      </p>

      {error && <div className="error-banner">{error}</div>}

      {calls.length === 0 ? (
        <p className="subtitle">Aun no hay llamadas registradas.</p>
      ) : (
        <div className="call-list">
          {calls.map((call) => (
            <CallCard key={call.id} call={call} />
          ))}
        </div>
      )}
    </>
  )
}

function CallCard({ call }: { call: CallListItem }) {
  const [expandedChunk, setExpandedChunk] = useState<string | null>(null)

  const signals: Array<[string, string | null]> = [
    ['pain_nrs', call.pain_nrs != null ? `${call.pain_nrs}/10` : null],
    ['fever_c', call.fever_c != null ? `${call.fever_c}°C` : null],
    ['mobility', call.mobility ?? null],
    ['wound', call.wound ?? null],
    ['appetite', call.appetite ?? null],
    ['sleep', call.sleep ?? null],
  ]

  return (
    <section className="card call-card">
      <div className="call-card-header">
        <div>
          <h2 style={{ margin: 0 }}>{call.patient_name ?? 'Paciente anonimo'}</h2>
          <p className="subtitle" style={{ margin: '4px 0 0' }}>
            {[
              call.category,
              call.postop_day != null ? `dia ${call.postop_day} post-operatorio` : null,
              call.age != null ? `${call.age} anos` : null,
            ]
              .filter(Boolean)
              .join(' · ') || 'Sin contexto de procedimiento'}
          </p>
          {call.comorbidities.length > 0 && (
            <p className="subtitle" style={{ margin: '4px 0 0' }}>
              Condiciones preexistentes: {call.comorbidities.join(', ')}
            </p>
          )}
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <span className={`status-pill ${call.status}`}>{call.status}</span>
          {call.final_triage && <span className={`status-pill ${call.final_triage}`}>{call.final_triage}</span>}
        </div>
      </div>

      {call.triage_rationale && <p className="triage-rationale">{call.triage_rationale}</p>}

      <div className="signal-grid">
        {signals.map(([key, value]) => (
          <div key={key} className="signal-chip">
            <span className="signal-label">{SIGNAL_LABELS_ES[key]}</span>
            <span className="signal-value">{value ?? '—'}</span>
          </div>
        ))}
      </div>

      {call.missing_info.length > 0 && (
        <p className="missing-info">
          Informacion faltante: {call.missing_info.join(', ')}
        </p>
      )}

      {call.pathology_assessment && (
        <div className="pathology-block">
          <h3>Validacion contra la base de conocimiento</h3>
          <p>{call.pathology_assessment}</p>
          {call.pathology_evidence.length > 0 && (
            <div className="evidence-chips">
              {call.pathology_evidence.map((ev) => (
                <button
                  key={ev.chunk_id}
                  type="button"
                  className="evidence-chip"
                  title={ev.chunk_id}
                  disabled={!ev.text}
                  onClick={() => setExpandedChunk((current) => (current === ev.chunk_id ? null : ev.chunk_id))}
                >
                  {ev.document_id.slice(0, 8)}
                  {ev.page != null ? ` · pag ${ev.page}` : ''}
                </button>
              ))}
            </div>
          )}

          {expandedChunk &&
            call.pathology_evidence
              .filter((ev) => ev.chunk_id === expandedChunk && ev.text)
              .map((ev) => (
                <blockquote key={ev.chunk_id} className="evidence-text">
                  {ev.text}
                </blockquote>
              ))}
        </div>
      )}
    </section>
  )
}
