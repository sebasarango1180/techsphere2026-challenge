# api-gateway

Control-plane REST API. Contract: [`../../docs/openapi/api-gateway.yaml`](../../docs/openapi/api-gateway.yaml),
rationale: [`../../specs/implementation-plan.md`](../../specs/implementation-plan.md) §2, §4.1.

```
cmd/api-gateway/main.go       entrypoint: runs migrations, wires config, db pool, router, starts listening
internal/config/               env var loading
internal/db/                   pgx pool setup (used by handlers; migrate/ uses its own short-lived connection)
internal/migrate/              golang-migrate runner -- applies infra/postgres/migrations/*.sql at startup
internal/models/               DTOs shared by the HTTP handlers
internal/httpapi/              one file per resource: documents.go, patients.go, calls.go, escalations.go, metrics.go, webhook.go
internal/livekitauth/          hand-rolled LiveKit JWT minting (see token.go docstring for why not the auth SDK package)
internal/livekitadmin/         RoomService.CreateRoom (patient context as room metadata) via the real generated
                                Twirp client (github.com/livekit/protocol/livekit) -- see room.go docstring for why
                                this ONE piece of the SDK is fine to depend on when internal/livekitauth's isn't
internal/vectorstore/          HTTP client for the vector-store service
```

## Migrations

Schema lives in `infra/postgres/migrations/*.up.sql` / `*.down.sql`, applied automatically
and idempotently by `internal/migrate` every time this process starts (via
[golang-migrate](https://github.com/golang-migrate/migrate), tracked in the
`schema_migrations` table it manages) -- **not** by Postgres's own
`docker-entrypoint-initdb.d`, which only ever runs once against an empty volume and
silently stops applying anything the moment a second migration file exists. Verified
end-to-end against a real Postgres container during development (fresh apply creates all
7 tables + `schema_migrations`; a second run against the same DB is a clean no-op).

Adding a schema change: add a new `000N_description.up.sql` / `.down.sql` pair, don't
edit an existing one once it's shipped.

This is also why the Docker build context for this service is the **repo root**, not this
directory -- see `Dockerfile`'s top comment.

## Status

Documents/patients/calls/escalations/metrics endpoints have real Postgres queries wired
in already, not just stubs -- `go build ./...` and `go run ./cmd/api-gateway` both work
against a running `postgres` + `vector-store`. The full patient -> call -> LiveKit room
metadata flow is verified end to end against a real Postgres AND a real LiveKit server
(not mocked): create a patient, create a call for them, and the room that actually exists
on LiveKit carries `{patient_id, patient_name, category, procedure, postop_day}` as its
metadata -- confirmed by querying LiveKit's own RoomService.ListRooms directly, not just
checking for the absence of an error.

The document create -> `PUT /documents/{id}` re-index -> version-supersession flow is
also verified live against a real Postgres + vector-store: upload, confirm searchable,
re-index with different content, confirm only the new version is searchable. That live
run caught a real bug worth knowing about: a refactor of `UploadDocument` had silently
dropped the `document_versions` INSERT -- the endpoint still reported success (vector-store's
own ingest genuinely succeeded), but `GET /documents/{id}/status` 404'd immediately after
because the bookkeeping row never existed. Fixed, and `ingestVersion`'s UPDATE now logs
loudly if it ever again matches zero rows (an `UPDATE` matching nothing isn't a SQL
error, which is exactly why this was silent the first time). Separately, that same
session of testing found `internal/vectorstore.Client`'s ingest call timing out at 60s
even though vector-store's embedding model just hadn't finished its one-time startup
load yet -- fixed on both sides: vector-store now pre-warms at startup (see its README),
and this client no longer sets a blanket `http.Client.Timeout` (which silently overrides
any *longer* per-call context deadline) in favor of explicit per-call timeouts sized to
what each operation actually needs.

CORS: both frontend apps call this API directly from the browser (two separate SPAs, not
one app with two routes -- plan §1), so without an allowlist every request fails at the
preflight before reaching a handler. Found live testing call-interface against a real
running api-gateway (`No 'Access-Control-Allow-Origin' header`) -- fixed via
`github.com/gin-contrib/cors` in `internal/httpapi/server.go`, allowlist from
`CORS_ORIGINS` (comma-separated; defaults to both Vite dev ports, `.env.example`),
verified live with a real preflight `OPTIONS` request carrying `Origin:
http://localhost:5173`.

What's still open, in priority order for G2/G3/G4/G5:

- [ ] `GET /healthz` should also check DB connectivity, not just process liveness
- [ ] `POST /internal/livekit/webhook`: verify the signed payload, parse
      `room_started`/`room_finished` into `calls.started_at`/`ended_at` (see webhook.go TODO)
- [ ] `MetricsSummary`'s `est_cost_per_call`: needs the pricing methodology decided (plan §0)
- [ ] Auth: deliberately none, matching the challenge's explicit exclusion of "autenticacion
      empresarial o gestion de roles" from required scope -- this is a scope decision, not
      an oversight; documented here and in the informe rather than silently shipped
- [ ] No admin-console UI exists yet to create/browse *patients* through (`GET /patients`
      is real and tested, but today only reachable via curl/Postman) -- the console does
      now have a "Llamadas" tab reading `GET /calls`, which joins `calls`/`patients`/
      `call_summaries` into one list (six signals, final triage, pathology validation) so
      the admin console doesn't need N+1 requests per call

## Run locally (outside Docker)

```sh
export DATABASE_URL=postgres://techsphere:changeme@localhost:5432/techsphere?sslmode=disable
export MIGRATIONS_PATH=../../infra/postgres/migrations
export LIVEKIT_API_KEY=devkey
export LIVEKIT_API_SECRET=changeme_min_32_chars_______________
go run ./cmd/api-gateway
```
