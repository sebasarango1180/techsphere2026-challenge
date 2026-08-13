# admin-console

The knowledge-management surface (ParticipantArtifacts/README.md's "consola de
administracion"): upload / list / delete documents, with a "procesado y disponible"
indicator. Standalone app, deliberately separate from
[`../call-interface`](../call-interface). Rationale:
[`../../specs/implementation-plan.md`](../../specs/implementation-plan.md) §1, §5.

```
src/api/client.ts   typed fetch wrapper for api-gateway's document endpoints
src/App.tsx          upload / list / delete documents, polls processing status
```

## Status

Builds and type-checks clean (`npm run build`), dev server verified to serve and hot-reload.
**Not yet tested against a running api-gateway** -- next step is exactly that, including
a G5 rehearsal (upload a document outside the given corpus, confirm it's usable, delete
it, confirm it's gone).

- [ ] Category input is free-text with suggestions; decide whether the five dataset
      categories should be a hard enum once real ingestion rules solidify
- [ ] No auth: deliberate scope decision (see root README.md), not an oversight -- the
      challenge explicitly excludes "autenticacion empresarial o gestion de roles"

## Run locally

```sh
npm install
npm run dev
```

Reads `VITE_API_GATEWAY_URL` from the environment (see `.env.example` at the repo root) --
**baked in at build time**, not read at container start; see `Dockerfile`'s comment if you
change it and nothing happens.
