// Minimal client for api-gateway -- this app only ever needs to start a call. Mirrors
// the relevant slice of docs/openapi/api-gateway.yaml /
// specs/implementation-plan.md §4.1.

const BASE_URL = import.meta.env.VITE_API_GATEWAY_URL ?? 'http://localhost:8080'

export interface CallCreated {
  call_id: string
  livekit_room: string
  livekit_token: string
}

// Optional context a caller can type in before starting -- no registered patient
// required (services/api-gateway/internal/httpapi/calls.go's CreateCall only uses these
// for an anonymous call, i.e. when no patient_id is sent, which this app never sends).
export interface CallContextInput {
  patient_name?: string
  age?: number
  comorbidities?: string[]
}

export const api = {
  async createCall(context?: CallContextInput): Promise<CallCreated> {
    const res = await fetch(`${BASE_URL}/api/v1/calls`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(context ?? {}),
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      throw new Error(`POST /calls failed: ${res.status} ${body}`)
    }
    return res.json()
  },
}
