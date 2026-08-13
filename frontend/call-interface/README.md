# call-interface

The patient-facing surface (ParticipantArtifacts/README.md's "interfaz de llamada"):
start a voice call from the browser, speak, listen to the agent. Standalone app,
deliberately separate from [`../admin-console`](../admin-console) -- different audience,
different (lack of) auth requirements. Rationale:
[`../../specs/implementation-plan.md`](../../specs/implementation-plan.md) §1, §5.

```
src/api/client.ts   minimal fetch wrapper -- this app only ever calls POST /calls
src/App.tsx          start call -> get LiveKit token from api-gateway -> join room -> reconnect handling
```

## Status

Builds and type-checks clean (`npm run build`), dev server verified to serve and hot-reload.
**Not yet tested against a running api-gateway + LiveKit** -- next step is exactly that,
end to end.

- [ ] Verify `POST /calls` -> `LiveKitRoom` connect flow against a real api-gateway + LiveKit
- [ ] Reconnect handling (see App.tsx's docstring) is implemented but only ever
      exercised by a synthetic `key` remount in dev -- verify it actually rejoins the same
      LiveKit room after a real network drop, not just after a manual "Reconectar" click
- [ ] No auth on this app at all -- deliberate: patients don't log in for a phone call,
      and the challenge explicitly excludes "autenticacion empresarial" from scope

## Run locally

```sh
npm install
npm run dev
```

Reads `VITE_API_GATEWAY_URL` / `VITE_LIVEKIT_URL` from the environment (see `.env.example`
at the repo root) -- **note these are baked in at build time**, not read at container
start; see `Dockerfile`'s comment if you change them and nothing happens.
