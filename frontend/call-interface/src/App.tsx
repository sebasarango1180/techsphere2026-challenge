import { useState } from 'react'
import {
  LiveKitRoom,
  RoomAudioRenderer,
  TrackToggle,
  DisconnectButton,
  useConnectionState,
  useVoiceAssistant,
  BarVisualizer,
  type AgentState,
} from '@livekit/components-react'
import { DisconnectReason, Track } from 'livekit-client'
import '@livekit/components-styles'
import './App.css'
import { api, type CallCreated } from './api/client'

const LIVEKIT_URL = import.meta.env.VITE_LIVEKIT_URL ?? 'ws://localhost:7880'

// Standalone patient-facing app (ParticipantArtifacts/README.md's "interfaz de llamada"),
// separate from the admin console app -- see ../admin-console. G4 is verified with a
// live greeting + trivial question exchange, so this has to actually connect to LiveKit,
// not just render a mock UI.
//
// Reconnect handling (specs/implementation-plan.md's "sessions" decision: a `calls` row
// IS the session; a transient network drop should resume the SAME session, not silently
// start a new one with an empty transcript). `onDisconnected` only fires once
// livekit-client's own reconnection attempts are exhausted, so by then there's nothing
// left to resume at the transport level -- what we do here is avoid minting a brand new
// call_id/room on a drop: we keep the existing token/room around and let the patient (or
// an auto-retry) rejoin the SAME room, so turns keep appending to the same Postgres
// `calls` row instead of starting a disconnected new one.
type Phase = 'idle' | 'connecting' | 'connected' | 'reconnect-needed'

export default function App() {
  const [phase, setPhase] = useState<Phase>('idle')
  const [call, setCall] = useState<CallCreated | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [error, setError] = useState<string | null>(null)

  // Optional pre-call context -- lets the agent address the patient by name and be
  // aware of their age/known conditions without needing a registered patient record
  // (services/api-gateway's CreateCall only applies these for an anonymous call, which
  // is all this app ever sends). Entirely optional: an empty name still starts a call
  // with no context, same as before this existed.
  const [patientName, setPatientName] = useState('')
  const [age, setAge] = useState('')
  const [conditions, setConditions] = useState('')

  async function startCall() {
    setPhase('connecting')
    setError(null)
    try {
      const created = await api.createCall({
        patient_name: patientName.trim() || undefined,
        age: age.trim() ? Number(age) : undefined,
        comorbidities: conditions.trim()
          ? conditions.split(',').map((c) => c.trim()).filter(Boolean)
          : undefined,
      })
      setCall(created)
      setAttempt((a) => a + 1)
      setPhase('connected')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setPhase('idle')
    }
  }

  function handleDisconnected(reason?: DisconnectReason) {
    if (reason === DisconnectReason.CLIENT_INITIATED || reason === undefined) {
      // Patient explicitly ended the call (or we don't know why -- treat as final).
      setCall(null)
      setPhase('idle')
      return
    }
    // Unexpected drop (network blip, server restart, etc). Keep `call` so a reconnect
    // rejoins the same LiveKit room / same call_id / same Postgres session.
    setPhase('reconnect-needed')
  }

  function reconnect() {
    setAttempt((a) => a + 1) // forces LiveKitRoom to remount and rejoin
    setPhase('connected')
  }

  function endCall() {
    setCall(null)
    setPhase('idle')
  }

  if (phase === 'idle' || phase === 'connecting') {
    return (
      <div className="app-shell">
        <div className="call-stage">
          <h1>Llamada de seguimiento</h1>
          <p className="subtitle">Presiona el boton para iniciar tu llamada de voz con el asistente.</p>
          {error && <div className="error-banner">{error}</div>}
          <form
            className="precall-form"
            onSubmit={(e) => {
              e.preventDefault()
              startCall()
            }}
          >
            <p className="precall-hint">Opcional: cuentanos un poco sobre ti antes de empezar.</p>
            <input
              type="text"
              placeholder="Tu nombre"
              value={patientName}
              onChange={(e) => setPatientName(e.target.value)}
              disabled={phase === 'connecting'}
            />
            <input
              type="number"
              min={0}
              max={120}
              placeholder="Edad"
              value={age}
              onChange={(e) => setAge(e.target.value)}
              disabled={phase === 'connecting'}
            />
            <input
              type="text"
              placeholder="Condiciones preexistentes (ej. diabetes, hipertension)"
              value={conditions}
              onChange={(e) => setConditions(e.target.value)}
              disabled={phase === 'connecting'}
            />
            <button type="submit" className="big-button" disabled={phase === 'connecting'}>
              {phase === 'connecting' ? 'Conectando...' : 'Iniciar llamada'}
            </button>
          </form>
        </div>
      </div>
    )
  }

  if (phase === 'reconnect-needed' && call) {
    return (
      <div className="app-shell">
        <div className="call-stage">
          <h1>Se perdio la conexion</h1>
          <p className="subtitle">Tu llamada sigue activa. Presiona para reconectar.</p>
          <div className="call-controls">
            <button type="button" className="big-button" onClick={reconnect}>
              Reconectar
            </button>
            <button type="button" className="big-button danger" onClick={endCall}>
              Terminar llamada
            </button>
          </div>
        </div>
      </div>
    )
  }

  if (!call) return null

  return (
    <div className="app-shell">
      <LiveKitRoom
        key={attempt}
        serverUrl={LIVEKIT_URL}
        token={call.livekit_token}
        audio
        connect
        onDisconnected={handleDisconnected}
        data-lk-theme="default"
      >
        <ActiveCall />
        <RoomAudioRenderer />
      </LiveKitRoom>
    </div>
  )
}

// Spanish labels for @livekit/components-react's AgentState (useVoiceAssistant) -- this
// is what turns "the greeting may take a moment" from a silent wait into a visible,
// continuously-updating status, all the way from the moment the room connects through
// the agent's prewarm/greeting to normal turn-taking.
const STATE_LABELS_ES: Partial<Record<AgentState, string>> = {
  connecting: 'Conectando con tu asistente',
  'pre-connect-buffering': 'Conectando con tu asistente',
  initializing: 'Preparando tu asistente',
  idle: 'Preparando tu asistente',
  listening: 'Escuchando',
  thinking: 'Pensando',
  speaking: 'Hablando',
  disconnected: 'Desconectado',
  failed: 'No se pudo conectar',
}

const WAITING_STATES: AgentState[] = ['connecting', 'pre-connect-buffering', 'initializing', 'idle']

function ActiveCall() {
  const connectionState = useConnectionState()
  const { state, audioTrack } = useVoiceAssistant()
  const label = STATE_LABELS_ES[state] ?? 'Conectando con tu asistente'
  const waiting = WAITING_STATES.includes(state)

  return (
    <div className="call-stage">
      <h1>Llamada en curso</h1>

      <div className="agent-presence">
        <div className={`agent-orb-wrap state-${state}`}>
          <div className="agent-orb" />
        </div>
        <BarVisualizer state={state} trackRef={audioTrack} barCount={5} className="agent-visualizer" />
        <p className="agent-state-label">
          {waiting && <span className="dot" />}
          {label}
          {waiting && '...'}
        </p>
      </div>

      <p className="call-status">Conexion: {connectionState}</p>

      <div className="call-controls">
        <TrackToggle source={Track.Source.Microphone} className="big-button secondary">
          Silenciar microfono
        </TrackToggle>
        <DisconnectButton className="big-button danger">Terminar llamada</DisconnectButton>
      </div>
    </div>
  )
}
