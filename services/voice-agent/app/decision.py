"""Hybrid escalation logic (plan §2.3, docs/decision-flow.md). Two independent signals
feed a fusion rule that can only escalate, never downgrade:

  1. Track B: the model's own structured classification (produced elsewhere in the
     pipeline via Ollama JSON mode -- see pipeline TODO in app/main.py).
  2. This module: a deterministic keyword/threshold rule layer over the raw transcript,
     independent of anything the LLM says.

CLINICAL DISCLAIMER: not reviewed by a clinician. Given the rubric's explicit
"asymmetric risk" framing (a missed escalation is the worst failure mode -- plan §0),
treat this table as a safety-net floor, not a finished clinical instrument.

WHAT BELONGS IN THIS TABLE, AND WHAT DOESN'T -- a deliberate scope boundary, arrived at
after a real mistake: a first pass at this table tried to catch clinical *descriptions*
via regex (wound infection described as "un líquido, amarillo creo, saliendo ahí de la
herida" -- never "pus" or "mal olor"; confusion described as "se me olvida si fue ayer o
hace tres días"). Expanding the patterns to catch those specific phrasings worked, but
it's the wrong strategy: "lenguaje cotidiano, ambiguo y regional" (the challenge's own
framing) is an unbounded paraphrase space, and chasing it with regex is a losing game --
every fix catches one more phrasing and misses the next one. That kind of pattern
recognition is what Track B (the model, grounded by retrieved clinical context) is
actually good at; a regex safety net trying to do the same job just adds false-positive
surface area without closing the gap it's chasing.

So this table is scoped to what a keyword/threshold check is *reliable* at, not
comprehensive at:
  - objective numeric thresholds (an explicit temperature, e.g. "39.5")
  - structurally rigid absence-statements ("no orino", "no defeco", "no puedo apoyar" --
    low paraphrase risk, patients state these plainly)
  - emergency vocabulary with little ambiguity (breathing difficulty, chest pain, heavy
    bleeding, confusion/disorientation as stated words -- not inferred from context)
  - narrow, non-obvious domain correlations worth hard-coding precisely because a small
    model might not reliably surface them unless the RAG context happens to (e.g.
    shoulder-tip pain as a referred-pain sign after cholecystectomy)

Free-text symptom-*description* pattern matching (what does wound discharge sound like
in lay speech, what does confusion sound like) is deliberately NOT here anymore -- that's
Track B's job, backed by retrieval grounded in the actual clinical corpus.

KNOWN LIMITATION -- negation: `_is_negated` below is a shallow "does a negation word
appear shortly before the match" heuristic (catches "no he tenido fiebre"), not real
negation-scope parsing (a NegEx-style algorithm would handle "aunque no tuve fiebre ayer,
hoy sí" correctly; this doesn't). Verified it fixes a concrete false positive found while
building this table (plain "no ha tenido fiebre" was firing the fever rule before this
existed) -- treat it as a partial fix, not a solved problem.
"""

import re
from dataclasses import dataclass

TRIAGE_ORDER = {"verde": 0, "amarillo": 1, "rojo": 2}

_NEGATION_RE = re.compile(r"\b(no|sin|niega|negativo para)\b", re.I)


def _is_negated(text: str, match_start: int, window: int = 25) -> bool:
    """True if a negation cue appears in the `window` characters immediately before a
    match -- see the module docstring's KNOWN LIMITATION note before trusting this for
    anything beyond the shallow cases it was written for."""
    preceding = text[max(0, match_start - window) : match_start]
    return bool(_NEGATION_RE.search(preceding))


@dataclass
class RuleMatch:
    level: str
    rationale: str


# (regex, level, rationale, category_or_None_for_global, check_negation)
#
# check_negation=False marks rules where the word "no" is already part of the positive
# signal itself (absence-of-X rules: no orino, no defeco, no puedo apoyar) -- running
# the negation-window check on those would suppress their OWN trigger word (e.g. "no,
# no orino bien" would otherwise see the first "no," in the pre-match window and
# incorrectly cancel the second "no orino" match). Everything else defaults to True.
_RULES: list[tuple[re.Pattern, str, str, str | None, bool]] = [
    # --- Objective numeric threshold ---
    (re.compile(r"\bfiebre\b.*\b(3[9]|4[0-9])\b|\b(3[9]|4[0-9])\s*(grados|°)\b", re.I),
     "rojo", "Fiebre >=39C reportada", None, True),

    # --- Emergency vocabulary, low ambiguity ---
    (re.compile(r"sangr(ado|e).*(mucho|abundante|no para|empapad)", re.I),
     "rojo", "Sangrado abundante o incontrolado", None, True),
    (re.compile(r"\b(dificultad para respirar|falta de aire|no puedo respirar|dolor en el pecho)\b", re.I),
     "rojo", "Dificultad respiratoria o dolor toracico", None, True),
    (re.compile(r"\b(confus[ao]|desorientad[ao]|no reconoce|no responde bien)\b", re.I),
     "rojo", "Confusion o desorientacion reportada por el paciente o acompanante", None, True),
    (re.compile(r"herida.*(abiert|separad)", re.I),
     "rojo", "Posible dehiscencia de la herida quirurgica", None, True),
    (re.compile(r"\bvomit", re.I),
     "amarillo", "Vomito reportado", None, True),

    # --- Structurally rigid absence-statements ---
    (re.compile(r"\bno\s+(orino|he orinado|orina)\b", re.I),
     "amarillo", "Ausencia de miccion reportada", None, False),

    # --- Category-specific: narrow, non-obvious domain correlations ---
    (re.compile(r"\bdolor\b.*\b(hombro|espalda)\b", re.I),
     "amarillo", "Dolor referido a hombro/espalda (posible irritacion peritoneal)", "cholecystitis", True),
    (re.compile(r"\b(hinchad[ao]|caliente|enrojecid[ao])\b.*\b(rodilla|cadera)\b", re.I),
     "amarillo", "Signos inflamatorios en la articulacion protesica", "total joint replacement", True),
    (re.compile(r"\b(no puedo apoyar|no aguanta el peso)\b", re.I),
     "amarillo", "Incapacidad para apoyar la extremidad operada", "total joint replacement", False),
    (re.compile(r"\b(sangrado|flujo).*(vaginal|mama|seno)\b", re.I),
     "amarillo", "Sangrado o flujo anormal en el sitio quirurgico", "breast_cancer", True),
    (re.compile(r"\bno\s+(defeco|he defecado|deposicion)\b", re.I),
     "amarillo", "Ausencia de deposiciones reportada (post cirugia colorrectal)", "colorectal cancer", False),
]

# Deliberately removed from this table (see module docstring): a bare "fiebre reportada"
# catch-all with no threshold, and free-text wound-discharge/confusion-description
# pattern matching. Both are exactly the class of thing Track B + retrieval grounding
# should catch instead -- see app/clinical_snapshot.py's `wound`/`fever_c` fields, which
# is where that information now actually lives (structured, from the model, not from a
# regex trying to enumerate every way a patient might phrase it).


def rule_based_triage(text: str, category: str | None = None) -> RuleMatch | None:
    """Returns the highest-severity rule match against the raw transcript, or None if no
    rule fired -- None means "the rule layer has no opinion", never "verde"; the rule
    layer only ever pushes severity up (see fuse() below)."""
    best: RuleMatch | None = None
    for pattern, level, rationale, rule_category, check_negation in _RULES:
        if rule_category is not None and rule_category != category:
            continue
        match = pattern.search(text)
        if match and not (check_negation and _is_negated(text, match.start())):
            if best is None or TRIAGE_ORDER[level] > TRIAGE_ORDER[best.level]:
                best = RuleMatch(level=level, rationale=rationale)
    return best


@dataclass
class FusedDecision:
    level: str
    triggered_by: str  # "model" | "rule" | "both"
    rationale: str


def fuse(model_level: str, model_rationale: str, rule_match: RuleMatch | None) -> FusedDecision:
    """final_triage = max(model, rule) -- plan §2.3 point 4. The rule layer can only
    raise the severity the model already proposed, never lower it."""
    if rule_match is None:
        return FusedDecision(level=model_level, triggered_by="model", rationale=model_rationale)

    if TRIAGE_ORDER[rule_match.level] > TRIAGE_ORDER[model_level]:
        return FusedDecision(level=rule_match.level, triggered_by="rule", rationale=rule_match.rationale)

    if TRIAGE_ORDER[rule_match.level] == TRIAGE_ORDER[model_level]:
        return FusedDecision(
            level=model_level,
            triggered_by="both",
            rationale=f"{model_rationale} | regla: {rule_match.rationale}",
        )

    return FusedDecision(level=model_level, triggered_by="model", rationale=model_rationale)
