# Tech Sphere 2026 - Reto Tecnico

Agente conversacional de voz para seguimiento post-quirurgico. Construido contra las
guias y la rubrica del reto en
[`TechSphere2026/ParticipantArtifacts`](https://github.com/TechSphere2026/ParticipantArtifacts).

Plan de implementacion completo, con las decisiones de arquitectura y su justificacion:
[`specs/implementation-plan.md`](specs/implementation-plan.md). Diagrama de arquitectura
y de flujo de decision: [`docs/architecture.md`](docs/architecture.md) /
[`docs/decision-flow.md`](docs/decision-flow.md).

> **Estado:** en construccion. Esta seccion se actualiza a medida que cada servicio pasa
> de scaffolding a funcional -- ver el `README.md` de cada servicio en `services/*/` para
> el estado real pieza por pieza.

## Levantar el proyecto

```sh
git clone <este-repo>
cd techsphere2026-challenge
./scripts/setup.sh
```

Detecta el sistema operativo y el hardware disponible (ver
[`specs/implementation-plan.md`](specs/implementation-plan.md) §2.5 para el porque del
modo nativo en macOS) y levanta todo: base de datos, LiveKit, Chroma, vector-store,
modelo, api-gateway y frontend. En macOS, `ollama`, `voice-agent`, y **`vector-store`**
corren nativos en el host (no en Docker) para tener acceso a Metal -- Docker Desktop no
puede pasar a traves de Metal bajo ninguna circunstancia, y BGE-M3 (el modelo de
embeddings) resulto ser CPU-only incluso en modo nativo hasta que se movio vector-store
fuera de Docker tambien. **Chroma en si NO se movio** -- corre como su propio contenedor
(imagen oficial `chromadb/chroma`) en ambos modos, igual que postgres/livekit, porque no
hace ningun trabajo de GPU/Metal; vector-store (nativo o no, segun el modo) le habla por
HTTP. Ver `services/vector-store/README.md` para el detalle completo, incluyendo un error
real de diseño encontrado en el camino: mover TODO el proceso de vector-store nativo
tambien saco a Chroma de Docker (una libreria embebida sigue al proceso que la abre),
aunque Chroma no lo necesitaba.

- Interfaz de llamada (paciente): http://localhost:5173
- Consola de administracion: http://localhost:5174
- Documentacion de la API (OpenAPI): [`docs/openapi/`](docs/openapi/)

Son dos aplicaciones separadas (no una sola con dos rutas): distinta audiencia, distinta
postura de autenticacion a futuro. Ver
[`specs/implementation-plan.md`](specs/implementation-plan.md) §1.

**Tiempo de levantamiento (compuerta G2, limite 15 min):** _pendiente de medir en una
maquina limpia -- ver `specs/implementation-plan.md` §9 antes de reportar este numero en
la entrega final._

`setup.sh` carga la base de conocimiento (`scripts/bulk_ingest_corpus.py`, log en
`bulk_ingest.log`) de forma **bloqueante**, como parte del mismo levantamiento cronometrado
-- un sistema que no puede responder con la base de conocimiento no esta realmente
"corriendo y accesible" todavia. Ver `specs/implementation-plan.md` §8 para el
razonamiento completo y `services/vector-store/README.md` para las cifras reales medidas
(tres bugs reales encontrados y corregidos que hacian esto mucho mas lento -- o
directamente incorrecto -- de lo necesario: `/v1/ingest` bloqueaba el event loop de
vector-store; tanto los embeddings BGE-M3 como el cliente de ChromaDB resultaron no ser
thread-safe bajo concurrencia, no solo lentos sino con corrupcion real de requests; y
BGE-M3 corria en CPU incluso en modo nativo porque vector-store seguia en Docker, sin
acceso a Metal). **Medido en vivo, corpus completo de 107 PDFs:** ~90s/documento en
Docker/CPU (la arquitectura anterior) vs. **1449s totales (~24.2 min, ~13.5s/documento)
en nativo/Metal contra la arquitectura final** (Chroma como servidor propio, ver mas
abajo) -- 0 fallos.

Para no pagar ese costo en cada arranque, `scripts/export_kb_seed.sh` /
`import_kb_seed.sh` permiten precomputar el corpus dado UNA vez (filas de Postgres +
volumen Docker de Chroma) y restaurarlo en arranques futuros en vez de recalcular los
embeddings -- ver esos scripts para el detalle. No debilita G5 (la compuerta de
actualizacion en vivo de la base de conocimiento): esa se prueba con un documento que NO
esta en este seed, asi que el pipeline de ingesta real sigue teniendo que funcionar en
vivo de todas formas.

## Modelo declarado (compuerta G3)

**Phi-3.5-mini** (familia Microsoft Phi Mini, serie 3.5+), servido localmente via Ollama.
Ver [`specs/implementation-plan.md`](specs/implementation-plan.md) §2.1 para la
justificacion completa y el mecanismo de swap a otros proveedores permitidos.

## Metricas requeridas por la rubrica

_Pendientes de medir contra una sesion real -- ver
[`ParticipantArtifacts/docs/rubrica-evaluacion.md`](https://github.com/TechSphere2026/ParticipantArtifacts/blob/main/docs/rubrica-evaluacion.md#5-qu%C3%A9-debe-reportar-tu-readme)
§5. `GET /api/v1/metrics/summary` en api-gateway calcula estos numeros desde
`turns.stt_ms/retrieval_ms/llm_ms/tts_ms` en Postgres -- no reportar nada aqui que no
venga de ese endpoint / de los logs reales de una sesion._

| Metrica | Valor |
|---|---|
| Latencia P50 | _pendiente_ |
| Latencia P95 | _pendiente_ |
| Tokens de entrada/salida por turno | _pendiente_ |
| Tokens de entrada/salida por llamada | _pendiente_ |
| Invocaciones al modelo por turno | _pendiente_ |
| Consultas al RAG por llamada | _pendiente_ |
| Costo estimado por llamada | _pendiente_ |

## Estructura del repositorio

```
services/api-gateway/         API de control (Go + Gin) -- ver su README.md
services/vector-store/        Ingesta + busqueda hibrida (Python + FastAPI + ChromaDB)
services/voice-agent/         Pipeline de voz en tiempo real (Python + livekit-agents)
frontend/call-interface/      App del paciente: iniciar/hablar/escuchar (React + Vite)
frontend/admin-console/       App de administracion: subir/listar/eliminar documentos (React + Vite)
infra/                        Config de LiveKit, migraciones de Postgres
docs/                         Diagramas, especificaciones OpenAPI
specs/                        Plan de implementacion y spec original del reto
scripts/setup.sh              Bootstrap de un solo comando
scripts/bulk_ingest_corpus.py Carga masiva del corpus dado a la base de conocimiento (idempotente)
scripts/export_kb_seed.sh     Snapshot de un corpus ya cargado (Postgres + volumen Chroma) en un archivo local
scripts/import_kb_seed.sh     Restaura ese snapshot -- usado automaticamente por setup.sh si el archivo existe
```

## Desarrollo

Cada servicio gestiona sus propias dependencias (Go modules, `uv` para Python, `npm` para
las apps de frontend) -- ver el `README.md` de cada uno en `services/*/` y
`frontend/*/` para instrucciones de ejecucion local fuera de Docker.
