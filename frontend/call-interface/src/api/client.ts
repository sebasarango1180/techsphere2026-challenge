// Minimal client for api-gateway -- this app only ever needs to start a call. Mirrors
// the relevant slice of docs/openapi/api-gateway.yaml /
// specs/implementation-plan.md §4.1.

const BASE_URL = import.meta.env.VITE_API_GATEWAY_URL ?? 'http://localhost:8080'

export interface CallCreated {
  call_id: string
  livekit_room: string
  livekit_token: string
}

export const api = {
  async createCall(): Promise<CallCreated> {
    const res = await fetch(`${BASE_URL}/api/v1/calls`, { method: 'POST' })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      throw new Error(`POST /calls failed: ${res.status} ${body}`)
    }
    return res.json()
  },
}
