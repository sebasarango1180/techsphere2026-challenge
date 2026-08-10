# Dataset EDA

Analysis of the challenge dataset (`dataset/` at repo root — copied locally from
`ParticipantArtifacts/dataset/` for inspection, **not committed**; see note at the bottom).
Goal: understand how conversations and clinical references are actually structured, so
`infra/postgres/migrations/0001_init.sql` and the retrieval/decision design in
`specs/implementation-plan.md` match reality instead of assumption.

Every number below comes from actually loading the files (pandas/openpyxl/pypdf), not
from the `ParticipantArtifacts/README.md` description alone — a few things below
contradict or add detail beyond that README.

## 1. The four tables, actually inspected

| File | Shape | Grain |
|---|---|---|
| `dataset_final.xlsx` | 3991 rows × 13 cols | one row = **one conversation turn** |
| `trayectorias_postop_silver.xlsx` | 160 rows × 12 cols | one row = one patient × one postop day (= one `caso`) |
| `perfiles_clinicos_pacientes_silver_contest.xlsx` | 40 rows × 11 cols | one row = one patient's clinical record |
| `perfiles_pacientes_co.xlsx` | 40 rows × 11 cols | one row = one patient's Colombian demographic profile |

40 patients × 4 postop days (**1, 3, 7, 14** — exactly these four, nothing else) = 160
`caso`s = 160 rows in the trajectories file. `dataset_final.xlsx`'s `caso_id` and
`paciente_id`/`dia_postop` are already denormalized onto every turn row — you don't
actually need to join through `trayectoria_id` to know which patient/day a turn belongs
to, only to pull the trajectory's structured ground truth (§3).

## 2. Conversation structure (`dataset_final.xlsx`)

Columns: `dialogo_id, caso_id, paciente_id, dia_postop, turno_idx, hablante, texto,
label_ground_truth, estilo_paciente, modelo_paciente, modelo_agente, capa, generado_ts`.

**Every `capa1_limpia` conversation has exactly 12 turns — always the same 6-question
structure, in the same order, for every single case regardless of procedure:**

1. Pain (0–10 scale)
2. Fever / chills
3. Mobility / ability to walk
4. Surgical wound appearance
5. Appetite
6. Sleep

`std=0.0` on turns-per-case — this isn't a loose pattern, it's rigid. **The procedure/
category is never once mentioned in any of the 3991 turns** (checked with keyword
matching for apendic-/colecist-/colectom-/mastecto-/reemplazo/artroplastia — zero hits).
The agent's questions are 100% procedure-agnostic; it never says "since your
appendectomy...". This is the single most load-bearing fact for §7's schema question
below.

`hablante` has **three** values, not two: `agente` (1920), `paciente` (1920), and
`tercero` (151) — a family member interjecting, only in `capa2_ruidosa`. Not mentioned
by name in the challenge README's prose, but consistent with its "interrupciones de un
familiar" description.

### capa1 (clean) vs capa2 (noisy), same case, side by side

```
capa1                                              capa2
[agente]   ¿Cómo ha estado el dolor... 0 al 10?     [agente]   ¿Cómo ha estado el dolor... 0 al 10?
[paciente] La verdad, el dolor ha sido más bien      [paciente] Este... no, nada, siga con la otra
           un 1, apenas se nota, casi nada.                     pregunta.
```

The patient didn't just get noisier phrasing — **the pain answer is deflected entirely**,
information genuinely lost for that turn. Later in the same capa2 conversation:

```
[agente]   ¿Cómo ha estado su apeti- desd- la cirugía?
[paciente] Normal, he comido como siempre, sin novedades. ... Puede ser, no estoy seguro.
[agente]   ¿Cómo ha estado durmiendo...? Bueno, eso dije, pero ayer le dije lo contrario.
[paciente] He dormido bien, tranquilo, apenas un poc- [inaudible] incomodidad al
           acostarme, pe- en general nor- com- siempre.
```

Corruption isn't limited to patient turns — the *agent's own line* gets a self-
contradiction stub spliced in ("eso dije, pero ayer le dije lo contrario"). Degradation
modes seen: mid-word cutoffs, `[inaudible]` markers, appended uncertainty ("no estoy
seguro"), outright deflection, and self-contradiction. This directly validates the
plan's §2.3 design choice (ask a clarifying question instead of guessing when a signal
is missing) — capa2 produces exactly the kind of gap that behavior exists for, not a
hypothetical.

`estilo_paciente` (5 archetypes, roughly evenly distributed): `minimizador_sintomas`,
`confundido`, `colaborativo`, `evasivo`, `ansioso`. Useful both as prompt-tuning material
(§2.7's "iterate against real transcripts" TODO) and as ready-made adversarial-persona
test cases for the demo/informe.

## 3. Clinical ground truth (`trayectorias_postop_silver.xlsx`)

This is the **hidden structured state** the conversation is a (sometimes noisy) natural-
language rendering of — six signals per case:

| Signal | Values | Distribution |
|---|---|---|
| `dolor_nrs` | 0–10 integer | — |
| `fiebre_c` | °C, continuous | — |
| `movilidad` | `normal` (95) · `limitada_esperada` (61) · `incapacitante_nueva` (4) | |
| `herida` | `normal` (118) · `eritema_leve` (39) · `secrecion_purulenta` (3) | |
| `apetito` | `normal` (97) · `levemente_disminuido` (34) · `muy_disminuido` (29) | |
| `sueno` | `normal` (95) · `levemente_alterado` (33) · `muy_alterado` (32) | |

`arquetipo_trayectoria` (3 values) correlates with but does **not** determine
`label_ground_truth` — cross-tab:

| label \ arquetipo | complicacion_leve_vigilancia | complicacion_real | recuperacion_normal |
|---|---:|---:|---:|
| verde | 41 | **7** | 75 |
| amarillo | 19 | 5 | 1 |
| rojo | 0 | 12 | 0 |

Seven `complicacion_real` cases still land on `verde` overall. Read together with the
per-archetype `dolor_nrs`/`fiebre_c` stats (`recuperacion_normal` fiebre_c: mean 36.7,
max 37.2 · `complicacion_real`: mean 37.86, max 39.5, **min 37.0**), this means
`label_ground_truth` was computed from the six structured signals by some threshold/rule
logic the dataset authors used — not a single symptom, and not deterministically from
the archetype label alone. That's independent validation of the plan's §2.3 hybrid
(model + deterministic rule, multi-signal) design, not just a nice-to-have.

**A concrete gap this surfaced in `services/voice-agent/app/decision.py`:** a real `rojo`
case's capa1 transcript describes wound discharge as *"mi hija me dijo que vio como un
líquido, amarillo creo, saliendo ahí de la herida"* — never the words "pus" or "mal olor"
that the current rule regex `(pus|mal olor|olor fuerte).*(herida|incision)` looks for.
That rule would **miss this real example**. Same transcript also has orientation
confusion ("se me olvida si fue ayer o hace tres días la operación") that isn't covered
by the existing confusion rule (`confus[ao]|desorientad[ao]|no reconoce|no responde
bien`) at all. Worth widening both before trusting this table operationally — it was
already flagged there as clinically unreviewed; this is a concrete instance of why.

## 4. Patient profile (`perfiles_clinicos_pacientes_silver_contest.xlsx`)

`modulo_synthea` is the clean category key — exactly 5 values, 8 patients each:
`appendicitis`, `cholecystitis`, `colorectal_cancer`, `total_joint_replacement`,
`breast_cancer`. This is the same taxonomy as `dataset/textos/`'s five folders (mind the
`_` vs space and casing differences: `colorectal_cancer` ↔ `colorectal cancer`,
`total_joint_replacement` ↔ `total joint replacement`, `Appendicitis` capitalized).
`procedimiento` is the human-readable Spanish name (`Apendicectomía`, `Colecistectomía`,
`Colectomía`, `Mastectomía`, `Reemplazo de cadera/rodilla`) — one fixed value per
category, not free text.

`comorbilidades` is a JSON array **inside a text cell**, exactly as the challenge README
warns (`["hipertension"]`, `["obesidad","diabetes_tipo_2"]`, 20/40 patients have none:
`[]`) — needs `json.loads` on ingest, not naive string splitting.

## 5. A real content mismatch worth designing around: `breast_cancer`

The `breast_cancer` folder in `dataset/textos/` (19 PDFs) contains **zero mastectomy or
breast-cancer-specific material**. Every filename and every readable document is about
**cervical cancer** ("cáncer de cuello uterino"): `002-GUIA-DE-CANCER-DE-CUELLO-UTERINO.pdf`,
`Cáncer-de-Cuello-Uterino-mar_2022.pdf`, `cervical-es-patient.pdf`,
`...Bhatla...Cancer of the cervix uteri...pdf`, etc. Checked the first 3 pages of every
file in the folder for "mastect"/breast mentions — none. Meanwhile
`perfiles_clinicos_pacientes_silver_contest.xlsx` assigns `breast_cancer`-category
patients the procedure `Mastectomía`.

This could be an intentional trap (the challenge README does warn "habrá conocimiento
clínico que tu agente no habrá visto antes") or a labeling artifact from how the corpus
was assembled — either way, it's a real property of the graded corpus, not a hypothetical:

- **A hard `where: {category: "breast_cancer"}` filter on retrieval will confidently
  return cervical-cancer content for a mastectomy patient's wound-care question** — wrong
  organ, wrong procedure, still clinically plausible-sounding. That's a worse failure
  mode than returning nothing, because it won't visibly look wrong.
- Softening category filtering (soft boost via RRF ranking rather than a hard Chroma
  `where` exclusion, or falling back to unscoped hybrid search when scoped search's top
  results score poorly) is worth considering for `services/vector-store/app/hybrid_search.py`
  specifically because of this finding, not as generic hardening.
- At minimum, the agent's "no tengo esa información" honesty fallback
  (`app/prompts.py`) is the safety net if scoped retrieval returns confidently-wrong
  content — another reason that fallback isn't optional polish.

## 6. Ingestion gotchas confirmed against the actual files (not hypothetical)

- **Scanned, no text layer**: exactly
  `dataset/textos/Appendicitis/REVISIÓN DE LA LITERATURA SOBRE LAAPENDICITIS AGUDA
  PEDIATRICA NO ESPECIFICADA EN EL PERI000 2000-2021.pdf` (1 page, `pypdf` extracts
  empty string) — the file the challenge README warns about, now identified by name.
  `services/vector-store/app/chunking.py` already logs a warning per empty page; OCR
  fallback is still a real TODO, not done.
- **New finding, not in the challenge README**: `dataset/textos/breast_cancer/
  Herramientas-Tecnica-Cancer-cuello-uterino-2018.pdf` is AES-encrypted and `pypdf`
  can't open it without the `cryptography` package (`cryptography>=3.1 is required for
  AES algorithm`) — currently not a dependency of `services/vector-store`.
  `app/main.py`'s broad `except Exception` around ingestion means this fails as one
  `document_versions.status='failed'` row rather than crashing the request, but it's a
  real failure the bulk-load of this corpus (plan §6 Phase 1) will hit, not a
  theoretical edge case. Add `cryptography` to `services/vector-store/pyproject.toml`.

## 7. What this means for the schema / the categories question

**Recommendation: categories are set patient-wise, established before the call — not
discovered through conversation.** This isn't a stylistic preference, it's what the
dataset itself does: the agent's fixed 6-question interview never references the
procedure, and the procedure lives only in the pre-existing clinical profile. That
matches how real post-surgical follow-up actually works — the clinic already knows what
surgery happened; the call is monitoring recovery, not intake. Concretely, this means:

- `patients` (already in `infra/postgres/migrations/0001_init.sql`, currently unused)
  needs to become a **real, populated table** — `procedure`/category is a first-class
  field on it, not an unknown the agent negotiates live.
- `POST /calls` needs to accept a `patient_id` (it currently takes no input at all).
- `api-gateway` needs to attach that patient's category to the LiveKit room as **room
  metadata** when it mints the room, which `voice-agent`'s `_extract_call_context()`
  would then read via `ctx.room.metadata` instead of hardcoding `None` (the exact TODO
  flagged earlier).
- This has a real UI implication: `admin-console` (or a seed-import script for the demo)
  needs a patient list/picker, and `call-interface`'s "Iniciar llamada" needs to become
  "iniciar llamada **para [patient]**" instead of an anonymous button — a product-shape
  change, not just a backend one. Worth confirming before building it.

**Important addendum, corrected after further discussion (worth stating explicitly since
it's easy to over-apply the recommendation above): "categories are patient-wise" answers
*who the patient is*, not *what the knowledge base is scoped to*. There is no per-patient
knowledge base "assignment" in this design at all — every call searches the same single,
current, versioned corpus. Category is used in exactly two places, both narrower than
"scope retrieval to this patient's corpus subset":**

1. A **soft ranking boost** (`category_hint` in `vector-store`'s `/v1/search`) — nudges
   ranking toward matching-category chunks, never excludes non-matching ones. Justified
   by the challenge's own framing (patients describe symptoms in "lenguaje cotidiano,
   ambiguo y regional" — they have no way to self-classify into the "right" category
   during a call) plus §5's finding that the category labels themselves aren't fully
   trustworthy. A hard filter on an untrustworthy label, for a patient who can't verify
   it either, is worse than no filter.
2. A **hard gate on category-specific rows in `decision.py`'s red-flag rule table** —
   this one stays hard, because it's a physiology fact ("ausencia de deposiciones" only
   means something post-colectomy), not a knowledge-retrieval-scoping question.

So: when a new patient arrives, nothing needs to happen to the knowledge base at all —
they immediately have full access to the same corpus every other call does. Only their
own profile (name, procedure, surgery date) needs to exist, for identification and for
rule #2 above.

**A secondary, smaller finding worth acting on regardless of the above:** the six
structured signals in §3 (`dolor_nrs`, `fiebre_c`, `movilidad`, `herida`, `apetito`,
`sueno`) are a better-defined target for what a call summary should extract than the
current freeform `call_summaries.symptoms_reported` text field. Worth considering
whether Track B's classification JSON (`app/prompts.py`'s `CLASSIFICATION_PROMPT_ES`)
should extract these six fields explicitly rather than just `{triage, confidence,
missing_info, citations}` — it would make summaries directly comparable to this
dataset's own ground truth format for eval purposes, essentially for free.

---

**Note on `dataset/` itself**: not committed — matches the plan's existing "don't vendor
the PDFs" decision (`specs/implementation-plan.md` §5), now also covering the two `.xlsx`
files. Added to `.gitignore`. `scripts/setup.sh` already clones the real source
(`ParticipantArtifacts`) at setup time; this local copy was only for this analysis.
