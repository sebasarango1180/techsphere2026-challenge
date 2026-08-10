import { useState } from 'react'
import {
  LiveKitRoom,
  RoomAudioRenderer,
  TrackToggle,
  DisconnectButton,
  useConnectionState,
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

  async function startCall() {
    setPhase('connecting')
    setError(null)
    try {
      const created = await api.createCall()
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
      <div className="call-stage">
        <h1>Llamada de seguimiento</h1>
        <p className="subtitle">
          Presiona el boton para iniciar tu llamada de voz con el asistente.
        </p>
        {error && <div className="error-banner">{error}</div>}
        <button type="button" className="big-button" onClick={startCall} disabled={phase === 'connecting'}>
          {phase === 'connecting' ? 'Conectando...' : 'Iniciar llamada'}
        </button>
      </div>
    )
  }

  if (phase === 'reconnect-needed' && call) {
    return (
      <div className="call-stage">
        <h1>Se perdio la conexion</h1>
        <p className="subtitle">Tu llamada sigue activa. Presiona para reconectar.</p>
        <div style={{ display: 'flex', gap: 16 }}>
          <button type="button" className="big-button" onClick={reconnect}>
            Reconectar
          </button>
          <button type="button" className="big-button danger" onClick={endCall}>
            Terminar llamada
          </button>
        </div>
      </div>
    )
  }

  if (!call) return null

  return (
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
  )
}

function ActiveCall() {
  const connectionState = useConnectionState()

  return (
    <div className="call-stage">
      <h1>Llamada en curso</h1>
      <p className="call-status">Estado: {connectionState}</p>
      <div style={{ display: 'flex', gap: 16 }}>
        <TrackToggle source={Track.Source.Microphone} className="big-button">
          Silenciar microfono
        </TrackToggle>
        <DisconnectButton className="big-button danger">Terminar llamada</DisconnectButton>
      </div>
    </div>
  )
}
