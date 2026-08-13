# Tech Sphere 2026 - Reto Tecnico

Agente conversacional de voz para seguimiento post-quirurgico. Construido contra las
guias y la rubrica del reto en
[`TechSphere2026/ParticipantArtifacts`](https://github.com/TechSphere2026/ParticipantArtifacts).

Plan de implementacion completo, con las decisiones de arquitectura y su justificacion:
[`specs/implementation-plan.md`](specs/implementation-plan.md). Diagrama de arquitectura
y de flujo de decision: [`docs/architecture.md`](docs/architecture.md) /
[`docs/decision-flow.md`](docs/decision-flow.md).

**Estado:** funcional de punta a punta -- llamada de voz en vivo (saludo, seis preguntas
en orden, red de seguridad determinista en tiempo real, clasificacion final +
validacion de patologia contra la base de conocimiento al cerrar), consola de
administracion con pestañas de Documentos y Llamadas, base de conocimiento cargable
desde cero o restaurable desde un snapshot precomputado. Ver el `README.md` de cada
servicio en `services/*/` para el detalle pieza por pieza.

## Desde cero: como levantar esto en una maquina nueva

Todo lo que sigue es lo que `./scripts/setup.sh` hace por vos -- esta seccion documenta
que instalar de antemano y que esperar mientras corre, no un procedimiento manual
alternativo.

### 1. Prerrequisitos (instalar antes de correr el script)

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
claramente, el agente vuelve a preguntar por el antes de despedirse. Al cerrar la
llamada -- ya sea porque el agente se despide o porque el paciente cuelga -- la
clasificacion final (los seis signos, el triage, y una validacion contra la base de
conocimiento con citas especificas) se calcula y se guarda sola; revisala en la pestaña
"Llamadas" de la consola de administracion.

### 6. Apagar / limpiar

```sh
docker compose --profile tqida down          # detiene los contenedores (conserva volumenes)
kill $(cat voice-agent.native.pid)           # solo en modo nativo -- ver setup.sh
kill $(cat vector-store.native.pid)          # idem
```

## Modelo declarado (compuerta G3)

**Phi-3.5-mini** (familia Microsoft Phi Mini, serie 3.5+), servido localmente via Ollama.
Ver [`specs/implementation-plan.md`](specs/implementation-plan.md) §2.1 para la
justificacion completa y el mecanismo de swap a otros proveedores permitidos.

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
