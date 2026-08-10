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
modo nativo en macOS) y levanta todo: base de datos, LiveKit, vector-store, modelo,
api-gateway y frontend.

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
razonamiento y las cifras reales medidas (incluye dos bugs reales encontrados y
corregidos que hacian esto mucho mas lento de lo necesario: `/v1/ingest` bloqueaba el
event loop de vector-store, y ejecutar embeddings BGE-M3 en paralelo los hacia ~13x mas
lentos por contencion de CPU, no mas rapidos).

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
```

## Desarrollo

Cada servicio gestiona sus propias dependencias (Go modules, `uv` para Python, `npm` para
las apps de frontend) -- ver el `README.md` de cada uno en `services/*/` y
`frontend/*/` para instrucciones de ejecucion local fuera de Docker.
