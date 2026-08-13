# Informe tecnico simplificado

Este documento junta, en un solo lugar, la "evidencia de tu proceso" que pide la rubrica
§5 (`ParticipantArtifacts/docs/rubrica-evaluacion.md`): que modelo se uso y por que,
donde estan los prompts reales, que configuraciones corrio el sistema, y donde ver el
demo. No repite contenido -- cada seccion apunta al archivo real correspondiente para que
lo mostrado aca nunca pueda desincronizarse de lo que el codigo realmente hace.

## Modelo elegido y por que

**Phi-3.5-mini (`phi3.5:3.8b`), servido 100% local via Ollama.** Ningun call a una API de
terceros para el LLM conversacional ni para la clasificacion/validacion de patologia --
las tres corren contra la misma instancia local de Ollama (`app/providers/llm.py`,
`app/main.py`'s `_classify_full_call`/`_validate_pathology`).

La razon es deliberada, no una limitacion: el objetivo era llevar un modelo pequeño lo
mas lejos posible sin salir del local, más qye elegir el modelo mas grande o mas capaz
disponible, sino ver cuanto se le puede exigir a uno menos de 4B corriendo en una macbook sin que se caiga, y resolver esos problemas de optimización con ingenieria alrededor del modelo (prompts
cortos, un solo signo/pregunta por llamada, gates de decision en vez de tool-calling
nativo, reintentos on-schema-miss) en vez de resolverlos cambiando a un modelo mas
grande. `specs/implementation-plan.md` §2.1 documenta el mecanismo de swap
(`LLMProvider`/`STTProvider`) que deja esto como una decision reversible, no un
compromiso permanente -- Gemini Flash y Llama via Groq son las alternativas ya
autorizadas por `stack-tecnico.md#1` si hiciera falta salir de local.

Consecuencia directa de esa eleccion, documentada en detalle en
[`docs/decision-flow.md`](decision-flow.md) y en los comentarios "found live" de
`app/main.py`/`app/prompts.py`: varios quiebres reales de un modelo de este tamaño (no
soporta tool-calling nativo, pierde adherencia a JSON schema cuando el prompt crece,
diverge en monologos si se le da contexto RAG sin acotar) y como se resolvieron sin
abandonar el modelo declarado. Esa lista de incidentes **es** la evidencia de que este
modelo se llevo hasta su limite real, no solo se declaro en el papel.

## Prompts

Todos los prompts que el sistema realmente usa viven en un solo archivo, en español
(los pacientes son hispanohablantes) y deliberadamente cortos -- ver el docstring del
modulo para el porque:

[`services/voice-agent/app/prompts.py`](../services/voice-agent/app/prompts.py)

| Prompt | Uso | Corre |
|---|---|---|
| `SYSTEM_PROMPT_ES` | Guia la conversacion (guion de seis temas, tono, limites) | Cada turno, streaming |
| `SEARCH_DECISION_PROMPT_ES` | Gate: ¿la frase del paciente es una pregunta clinica real? | Cada turno, aislado |
| `FINAL_CLASSIFICATION_PROMPT_ES` | Seis signos clinicos + triage + confianza, sobre la transcripcion completa | Una vez, al cerrar la llamada |
| `PATHOLOGY_VALIDATION_PROMPT_ES` | Correlaciona los signos contra el contexto RAG recuperado, con citas | Una vez, al cerrar la llamada (con reintento si el modelo omite una clave del JSON -- ver `app/main.py`'s `_validate_pathology`) |

`GREETING_ES` y `build_farewell()` (mismo archivo) se hablan verbatim via
`session.say()`, sin pasar por el LLM -- un modelo de este tamaño parafrasea texto que
tiene que ser exacto.

## Configuraciones

- [`.env.example`](../.env.example) -- toda variable de entorno que el sistema lee, con
  el porque de cada default documentado inline (modelo de Ollama, modo de STT, CORS,
  rutas de migraciones/OpenAPI).
- [`docker-compose.yml`](../docker-compose.yml) / `docker-compose.gpu.yml` -- perfiles
  `tqida` (siempre) / `docker-models` / `docker-agent` (solo modo Docker-only); ver el
  comentario superior de ese archivo para el porque de cada perfil.
- [`scripts/setup.sh`](../scripts/setup.sh) -- el unico camino de arranque documentado
  (README §"Desde cero"); auto-detecta modo nativo (macOS, Metal) vs Docker-only.
- `services/*/README.md` -- variables y comandos especificos de cada servicio para
  correrlo suelto, fuera de Docker.

## Metricas (evidencia cuantitativa)

Ver [`README.md`](../README.md#metricas-requeridas-por-la-rubrica) -- calculadas por
`GET /api/v1/metrics/summary` (`services/api-gateway/internal/httpapi/metrics.go`) desde
datos reales de Postgres, nunca tipeadas a mano. Ese mismo archivo documenta la
metodologia de extrapolacion de costo (referencia: pricing publico de
`llama-3.1-8b-instant` en Groq, mismo proveedor ya autorizado como alternativa de este
proyecto).

## Capturas del demo y respuestas a la rubrica

Videos del demo en vivo y las respuestas escritas a las preguntas que pide la rubrica
(fuera de este repo por tamaño/formato):
<https://drive.google.com/drive/folders/1G575086PAFGJu3zzndV_Wrp66sQBkD0t?usp=sharing>
