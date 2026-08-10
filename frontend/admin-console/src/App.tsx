import { useCallback, useEffect, useState } from 'react'
import { api, type DocumentSummary } from './api/client'
import './App.css'

// Standalone admin app (ParticipantArtifacts/README.md's "consola de administracion"),
// separate from the patient-facing call-interface app -- see ../call-interface. Required
// minimum contract: upload / list / delete / "processed and available" indicator. G5 is
// verified live with a document outside the given corpus, so the upload -> processing ->
// ready loop here has to actually work, not just look right.
//
// No auth: deliberate scope decision, matching the challenge's explicit exclusion of
// "autenticacion empresarial o gestion de roles" (see root README.md).
const CATEGORY_SUGGESTIONS = [
  'cholecystitis',
  'colorectal cancer',
  'appendicitis',
  'breast_cancer',
  'total joint replacement',
]

const POLL_INTERVAL_MS = 3000

export default function App() {
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
    <div className="app-main">
      <h1>Consola de administracion</h1>
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
    </div>
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
