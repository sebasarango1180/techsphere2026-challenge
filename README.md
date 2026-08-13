# Tech Sphere 2026 - Reto Tecnico

_tqida_ - Agente conversacional de voz para seguimiento post-quirurgico. Construido contra las
guias y la rubrica del reto en
[`TechSphere2026/ParticipantArtifacts`](https://github.com/TechSphere2026/ParticipantArtifacts).

Plan de implementacion completo, con las decisiones de arquitectura y su justificacion:
[`specs/implementation-plan.md`](specs/implementation-plan.md). Diagrama de arquitectura
y de flujo de decision: [`docs/architecture.md`](docs/architecture.md) /
[`docs/decision-flow.md`](docs/decision-flow.md).

**Estado:** funcional de punta a punta -- llamada de voz en vivo (saludo, seis preguntas
en orden, red de seguridad determinista en tiempo real, consulta a la base de
conocimiento EN CUALQUIER TURNO cuando el paciente hace una pregunta clinica real -- no
solo al cerrar, ver §"Decisiones clave" -- clasificacion final + validacion de patologia
contra la base de conocimiento al cerrar), consola de administracion con pestañas de
Documentos y Llamadas, base de conocimiento cargable desde cero o restaurable desde un
snapshot precomputado. Ver el `README.md` de cada servicio en `services/*/` para el
detalle pieza por pieza.

**Demo:** para ver los video demos, y las respuestas a las preguntas solicitadas en la rúbrica, descargar el material en Drive: https://drive.google.com/drive/folders/1G575086PAFGJu3zzndV_Wrp66sQBkD0t?usp=sharing

**Documentación:** para revisar las decisiones técnicas y documentación, creé la carpeta `docs/` con cada contenido organizado -- en particular [`docs/informe-tecnico.md`](docs/informe-tecnico.md) junta la evidencia de proceso que pide la rúbrica (prompts, configuraciones, y la declaración de qué modelo se usó y por qué).

## Desde cero: como levantar esto en una maquina nueva

Lo siguiente es lo que `./scripts/setup.sh` hace de forma automatica -- esta seccion documenta
qué instalar de antemano y qué esperar mientras corre, no un procedimiento manual
alternativo.

### 1. Prerrequisitos (instalar antes de correr el script)

- Asegurar una buena velocidad de descarga de internet, y suficiente memoria RAM y almacenamiento disponibles.

| Siempre necesario | Solo en macOS (modo nativo, para acceso a Metal) |
|---|---|
| [Docker](https://docs.docker.com/get-docker/) + Docker Compose v2 | [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (Python) |
| `git` | [`ollama`](https://ollama.com/download) (la app, o `ollama serve` corriendo) |
| | `tesseract` (`brew install tesseract tesseract-lang`) -- fallback de OCR de vector-store |

En Linux (o forzando `--docker-only` en cualquier SO) todo corre en contenedores, sin
prerrequisitos adicionales -- ver el porque del modo nativo en macOS en
[`specs/implementation-plan.md`](specs/implementation-plan.md) §2.5 y en
`docker-compose.yml`.

### 2. Un solo comando

```sh
git clone <este-repo>
cd techsphere2026-challenge
./scripts/setup.sh
```

Detecta el sistema operativo y el hardware disponible y levanta todo en paralelo: clona
el corpus dado (`ParticipantArtifacts/dataset`, si no esta ya presente como carpeta
hermana), construye las imagenes, descarga el modelo declarado (Phi-3.5-mini) via Ollama,
levanta Postgres + aplica las migraciones, restaura o carga la base de conocimiento (ver
§3 abajo), levanta Chroma, vector-store, api-gateway, LiveKit, y las dos apps de
frontend. Nada de esto requiere pasos manuales entre medio.

Flags disponibles:

```sh
./scripts/setup.sh                 # auto-detecta SO/hardware
./scripts/setup.sh --native-agent   # fuerza modo nativo (Ollama + voice-agent + vector-store en el host)
./scripts/setup.sh --docker-only    # fuerza todo en Docker (CPU only, util para CI/testing)
```

### 3. La base de conocimiento se carga sola -- no hay paso manual

`setup.sh` decide automaticamente, sin intervencion, entre dos caminos:

- **Si existe `kb-seed.tar.gz`** (un snapshot precomputado del corpus dado -- ver
  `scripts/export_kb_seed.sh`) **y la base de datos esta vacia:** lo restaura en
  segundos (filas de Postgres + volumen de Chroma), sin recalcular ningun embedding.
- **Si no existe, o la base de datos ya tiene datos:** corre la ingesta completa en vivo
  (`scripts/bulk_ingest_corpus.py`, OCR + embeddings BGE-M3 + Chroma), de forma
  **bloqueante** -- un sistema que no puede responder preguntas de la base de
  conocimiento no esta realmente "corriendo y accesible" todavia, asi que este tiempo
  cuenta dentro del levantamiento cronometrado (compuerta G2), no se corre en segundo
  plano por separado. Medido en vivo contra el corpus completo (107 PDFs, arquitectura
  final con Chroma como servidor propio): **~24 minutos, 0 fallos** -- ver
  `services/vector-store/README.md` para el detalle y los bugs reales que hacian esto
  mas lento de lo necesario antes de corregirse.

Este archivo `kb-seed.tar.gz` (~50MB, gitignored) no viene incluido en el repo por su
tamaño -- si lo tenes disponible (release de GitHub, almacenamiento propio, etc.),
colocalo en la raiz del repo antes de correr `setup.sh` para saltarte los ~24 minutos de
ingesta en vivo. Si no lo tenes, `setup.sh` simplemente hace la ingesta real -- no falla,
solo tarda mas. No debilita la compuerta G5 (actualizacion en vivo de la base de
conocimiento): esa se prueba con un documento que NO esta en este seed, asi que el
pipeline de ingesta real sigue teniendo que funcionar en vivo de todas formas.

### 4. Transcripcion de voz (STT): elegir una opcion antes de la primera llamada

Por defecto (`STT_MODE=groq` en `.env.example`) el agente usa la API de Groq para
transcribir la voz del paciente, lo que requiere una `GROQ_API_KEY` real (`.env` la trae
vacia). Dos opciones:

- Conseguir una clave gratuita en [Groq](https://console.groq.com/) y ponerla en `.env`, o
- Poner `STT_MODE=local` en `.env` antes de levantar el stack -- usa `faster-whisper`
  corriendo localmente, sin ninguna clave externa. Mas lento en CPU, pero funciona sin
  configuracion adicional y es lo usado durante el desarrollo/pruebas de este proyecto.

Sin una de las dos, la primera llamada real fallara al arrancar (`GROQ_API_KEY` vacia).

### 5. Probar que funciona

```
Interfaz de llamada (paciente):  http://localhost:5173
Consola de administracion:       http://localhost:5174
Documentacion de la API:         docs/openapi/ (o http://localhost:8080/docs si esta servido)
```

En la interfaz de llamada: opcionalmente completa tu nombre/edad/condiciones
preexistentes (el agente las usa para dirigirse a vos por nombre y tener contexto
clinico adicional), y presiona "Iniciar llamada". **El saludo puede tardar unos segundos
en la primera llamada** -- el proceso del agente recien arranca (carga de modelos VAD/TTS,
calentamiento de Ollama); la interfaz muestra un indicador visible ("Conectando con tu
asistente...") durante ese tramo, para que nunca parezca que la llamada esta congelada.
Una vez que el agente saluda, sigue un chequeo de seis temas siempre en el mismo orden
(dolor, fiebre, movilidad, herida, apetito, sueño); si alguno queda sin responder
claramente, el agente vuelve a preguntar por el antes de despedirse. **En cualquier
momento de la llamada**, si lo que decís no es solo una respuesta al guion sino una
pregunta clinica real (ej. "¿puedo bañarme con la herida así?"), el agente la responde
citando la base de conocimiento, o admite honestamente que no tiene esa informacion si
el corpus no la cubre -- ver "Decisiones clave" abajo para el mecanismo exacto (un gate
de decision por turno, no busqueda incondicional). Al cerrar la llamada -- ya sea porque
el agente se despide o porque el paciente cuelga -- la clasificacion final (los seis
signos, el triage, y una validacion contra la base de conocimiento con citas
especificas) se calcula y se guarda sola; revisala en la pestaña "Llamadas" de la
consola de administracion.

### 6. Apagar / limpiar

```sh
docker compose --profile tqida down          # detiene los contenedores (conserva volumenes)
kill $(cat voice-agent.native.pid)           # solo en modo nativo -- ver setup.sh
kill $(cat vector-store.native.pid)          # idem
```

## Modelo declarado (compuerta G3)

**Phi-3.5-mini** (`phi3.5:3.8b`, familia Microsoft Phi Mini, serie 3.5+), servido
localmente via Ollama -- para el LLM conversacional y para las tres llamadas de
clasificacion/validacion al cerrar la llamada (clasificacion final, validacion de
patologia, resumen narrativo). Ningun call a una API de terceros para el LLM.

**Por que este modelo:** el objetivo explicito fue llevar un modelo chico lo mas lejos
posible sin salir de lo local -- no elegir el mas grande/capaz disponible, sino ver
cuanto se le puede exigir a un ~3.8B en una laptop antes de que se rompa, y resolver esos
quiebres con ingenieria alrededor del modelo (prompts cortos, gates de decision en vez de
tool-calling nativo, reintentos on-schema-miss) en vez de cambiar a un modelo mas grande.
Ver [`docs/informe-tecnico.md`](docs/informe-tecnico.md) para la evidencia de proceso
completa y [`specs/implementation-plan.md`](specs/implementation-plan.md) §2.1 para el
mecanismo de swap a otros proveedores permitidos (Gemini Flash, Llama via Groq) si hiciera
falta salir de local.

## Decisiones clave encontradas y corregidas durante la implementacion

Documentadas aca porque cambiaron la arquitectura descrita en el plan original -- cada
una tiene el razonamiento completo en el archivo que se referencia, esto es solo el
resumen ejecutivo.

- **Chroma es su propio contenedor, en ambos modos** (no una libreria embebida dentro de
  vector-store). Una libreria embebida sigue al proceso que la abre -- mover
  vector-store fuera de Docker (necesario para Metal en macOS) sacaba a Chroma con el,
  aunque Chroma no hace ningun trabajo de GPU. Ver `docs/architecture.md`.
- **La clasificacion LLM se movio de por-turno a una sola pasada al final de la
  llamada.** La version original corria una clasificacion estructurada en cada turno,
  concurrente con la respuesta conversacional -- ambas competian por la misma instancia
  local de Ollama, causando timeouts y llamadas que se cortaban solas. Ver
  `docs/decision-flow.md`.
- **Validacion de patologia contra la base de conocimiento: llamada LLM separada de la
  clasificacion de los seis signos**, no campos extra en el mismo prompt -- combinarlas
  rompia la adherencia al schema JSON de un modelo de ~3.8B en cuanto tambien se agregaba
  contexto RAG al mismo prompt. Ver `docs/decision-flow.md`.
- **La busqueda en la base de conocimiento durante la llamada esta acotada por un gate de
  decision, no ausente ni incondicional.** Pasó por tres versiones: busqueda en cada
  turno (causo que el modelo divagara varios minutos sobre contenido de una categoria
  quirurgica totalmente distinta cuando la llamada no tenia categoria conocida),
  eliminarla por completo (rompia el requisito de la rubrica de respuestas clinicas
  trazables y demostrablemente basadas en el corpus), y finalmente una llamada LLM
  aislada que decide, por turno, si el paciente hizo una pregunta clinica real -- el
  modelo declarado no soporta "tool calling" nativo (confirmado: Ollama reporta
  `phi3.5:3.8b` con capacidades `["completion"]` unicamente), asi que esto es el
  equivalente practico. Ver `docs/decision-flow.md`.
- **Ronda de recuperacion (`makeup round`) antes de cerrar la llamada**: si el agente
  detecta, en una verificacion en vivo, que algun signo clinico quedo sin responder
  claramente, vuelve a preguntar especificamente por eso antes de despedirse -- acotado a
  un solo intento para que una respuesta ambigua nunca deje la llamada abierta
  indefinidamente.
- **El cierre de la llamada es responsabilidad del propio agente, no del cliente**: el
  agente dice la despedida y llama a `ctx.shutdown()` el mismo -- y ademas escucha el
  evento de cierre de la sesion (se dispara tambien si el paciente cuelga primero) para
  garantizar que la clasificacion final siempre se calcule y se guarde, sin importar quien
  termino la llamada.
- **La base de conocimiento se restaura desde un snapshot antes de intentar una ingesta
  en vivo**, con las migraciones de esquema corriendo temprano (via la imagen oficial
  `migrate/migrate`, antes de que Postgres o Chroma reciban trafico real) para que esa
  decision sea correcta incluso en una base de datos genuinamente nueva. Ver §3 arriba.

## Metricas requeridas por la rubrica

Medidas contra sesiones reales, no inventadas -- ver
[`ParticipantArtifacts/docs/rubrica-evaluacion.md`](https://github.com/TechSphere2026/ParticipantArtifacts/blob/main/docs/rubrica-evaluacion.md#5-qu%C3%A9-debe-reportar-tu-readme)
§5. `GET /api/v1/metrics/summary` en api-gateway calcula estos numeros desde
`turns.stt_ms/retrieval_ms/llm_ms/tts_ms/tokens_in/tokens_out` en Postgres (ver
`services/api-gateway/internal/httpapi/metrics.go`) -- no se reporta aca nada que no
venga de ese endpoint. Volvé a pegar la salida cruda de ese endpoint (o corré una sesion
nueva) antes de citar estos numeros en cualquier entrega -- son un snapshot de las
llamadas hechas hasta ahora en este entorno, no una constante del sistema.

| Metrica | Valor |
|---|---|
| Latencia P50 (turno agente, stt+retrieval+llm+tts) | 1814 ms |
| Latencia P95 (idem) | 10833.4 ms |
| Tokens de entrada/salida por turno (promedio) | 982.75 / 107.88 |
| Tokens de entrada/salida por llamada (promedio) | 873.56 / 95.89 |
| Invocaciones al modelo por turno | 1 (respuesta conversacional) + 1 si el gate de KB disparo esa vuelta -- ver "Decisiones clave"; al cerrar la llamada se suman, una sola vez, clasificacion final + validacion de patologia + resumen narrativo (3 llamadas fijas, no por turno) |
| Consultas al RAG por llamada (promedio) | 3.11 |
| Costo estimado por llamada | $0.0000513 USD -- metodologia: Phi-3.5-mini corre local, sin costo por token; este numero extrapola los tokens realmente medidos arriba contra el precio publico de `llama-3.1-8b-instant` en Groq (mismo proveedor/familia ya documentado como el swap-target permitido de este proyecto -- ver "Modelo declarado" abajo), $0.05 / 1M tokens entrada, $0.08 / 1M tokens salida (console.groq.com, agosto 2026). Constantes y cita completa en `metrics.go`. |

Snapshot real, capturado con `curl -s http://localhost:8080/api/v1/metrics/summary`
contra las llamadas de prueba hechas hasta ahora en este entorno -- volvé a correrlo
contra tu propia sesion antes de citar estos numeros en una entrega nueva, no son una
constante del sistema:

```json
{
  "p50_ms": 1814, "p95_ms": 10833.4,
  "tokens_in_per_turn": 982.75, "tokens_out_per_turn": 107.875,
  "tokens_in_per_call": 873.56, "tokens_out_per_call": 95.89,
  "rag_queries_per_call": 3.111, "est_cost_per_call": 0.0000513
}
```

## Estructura del repositorio

```
services/api-gateway/         API de control (Go + Gin) -- ver su README.md
services/vector-store/        Ingesta + busqueda hibrida (Python + FastAPI + Chroma via HTTP)
services/voice-agent/         Pipeline de voz en tiempo real (Python + livekit-agents)
frontend/call-interface/      App del paciente: contexto opcional, iniciar/hablar/escuchar (React + Vite)
frontend/admin-console/       App de administracion: Documentos + Llamadas (React + Vite)
infra/                        Config de LiveKit, migraciones de Postgres
docs/                         Diagramas de arquitectura/decision, especificaciones OpenAPI
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
