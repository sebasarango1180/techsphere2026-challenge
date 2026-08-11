"""The six-signal clinical picture for a call -- pain_nrs/fever_c/mobility/wound/
appetite/sleep, the exact taxonomy the reference dataset's trajectories use
(docs/dataset-eda.md §3, §7).

Extracted ONCE, at call end, from the full transcript (app/main.py's summarize_call,
via app/prompts.py's FINAL_CLASSIFICATION_PROMPT_ES) -- NOT incrementally per turn
anymore. That per-turn version existed earlier; it was reworked after finding, live,
that running that classification concurrently with the conversational LLM call on every
single turn contended for the same local Ollama instance and caused real timeouts and
dropped connections during live calls, and because a whole-transcript extraction is
more accurate than any single turn taken alone (a clarification given three turns later
can correct an earlier one). Conversation FLOW (which of the six topics to ask about
next) no longer depends on this snapshot being populated live -- see
`QUESTION_ORDER`/`topic_hint` below, driven by a simple turn counter in
`PostSurgicalAgent` instead.

Values the model returns are validated against the same enum vocabulary the DB columns
enforce (infra/postgres/migrations/0002_patient_context.up.sql) before being kept --
an out-of-vocabulary or out-of-range value from the model is dropped (logged, not
crashed on) rather than passed through to a database write that would reject it.
"""

import logging
from dataclasses import dataclass, fields

logger = logging.getLogger(__name__)

VALID_MOBILITY = {"normal", "limitada_esperada", "incapacitante_nueva"}
VALID_WOUND = {"normal", "eritema_leve", "secrecion_purulenta"}
VALID_APPETITE = {"normal", "levemente_disminuido", "muy_disminuido"}
VALID_SLEEP = {"normal", "levemente_alterado", "muy_alterado"}

_LABELS_ES = {
    "pain_nrs": "dolor",
    "fever_c": "temperatura",
    "mobility": "movilidad",
    "wound": "herida",
    "appetite": "apetito",
    "sleep": "sueno",
}

# Fixed order from docs/dataset-eda.md §2: every real conversation in the reference
# dataset asks these six in exactly this order, always -- "std=0.0 on turns-per-case",
# not a loose pattern the model happens to follow. Used to steer the LLM's NEXT question
# (see next_missing_topic below) rather than leaving ordering to its own judgment.
QUESTION_ORDER = ("pain_nrs", "fever_c", "mobility", "wound", "appetite", "sleep")

_TOPIC_HINTS_ES = {
    "pain_nrs": "el dolor, en una escala de 0 a 10",
    "fever_c": "si ha tenido fiebre o escalofrios",
    "mobility": "como se ha podido mover o caminar",
    "wound": "como se ve y se siente la herida",
    "appetite": "el apetito",
    "sleep": "como ha dormido",
}


def topic_hint(index: int) -> str | None:
    """Natural-language hint (Spanish) for QUESTION_ORDER[index], or None once index is
    past the last topic -- the turn-counter-driven replacement for the old
    snapshot-driven next_missing_topic() (see module docstring). A plain index lookup,
    deliberately not dependent on any live extraction succeeding."""
    if 0 <= index < len(QUESTION_ORDER):
        return _TOPIC_HINTS_ES[QUESTION_ORDER[index]]
    return None


@dataclass
class ClinicalSnapshot:
    pain_nrs: int | None = None
    fever_c: float | None = None
    mobility: str | None = None
    wound: str | None = None
    appetite: str | None = None
    sleep: str | None = None

    def merge(self, extracted: dict) -> None:
        """Applies whatever subset of the six fields Track B extracted this turn --
        fields it didn't mention are left untouched, never nulled out. Invalid values
        are dropped with a warning, not raised, since the caller shouldn't have a single
        malformed model response take down the rest of turn processing."""
        pain_nrs = extracted.get("pain_nrs")
        if isinstance(pain_nrs, (int, float)) and 0 <= pain_nrs <= 10:
            self.pain_nrs = int(pain_nrs)
        elif pain_nrs is not None:
            logger.warning("dropping out-of-range pain_nrs from model: %r", pain_nrs)

        fever_c = extracted.get("fever_c")
        if isinstance(fever_c, (int, float)) and 30.0 <= fever_c <= 45.0:
            self.fever_c = float(fever_c)
        elif fever_c is not None:
            logger.warning("dropping out-of-range fever_c from model: %r", fever_c)

        self._merge_enum("mobility", extracted.get("mobility"), VALID_MOBILITY)
        self._merge_enum("wound", extracted.get("wound"), VALID_WOUND)
        self._merge_enum("appetite", extracted.get("appetite"), VALID_APPETITE)
        self._merge_enum("sleep", extracted.get("sleep"), VALID_SLEEP)

    def _merge_enum(self, field: str, value: object, valid: set[str]) -> None:
        if value is None:
            return
        # Found live: the model reliably returns grammatically-natural Spanish with
        # spaces ("muy disminuido") instead of the enum's underscore form
        # ("muy_disminuido") -- an exact-match check was silently dropping otherwise-
        # correct extractions. Normalize before validating; store the canonical form.
        normalized = value.strip().lower().replace(" ", "_") if isinstance(value, str) else value
        if normalized in valid:
            setattr(self, field, normalized)
        else:
            logger.warning("dropping out-of-vocabulary %s from model: %r", field, value)

    def has_any(self) -> bool:
        return any(getattr(self, f.name) is not None for f in fields(self))

    def missing_fields(self) -> list[str]:
        """Every signal still null, in fixed order -- the fallback for call_summaries'
        `missing_info` when the model's own end-of-call response doesn't report any
        (app/main.py's summarize_call)."""
        return [_LABELS_ES[f] for f in QUESTION_ORDER if getattr(self, f) is None]

    def next_missing_topic(self) -> str | None:
        """Which of the six signals is still null, in fixed order -- None once all six
        are known. NOT used to drive conversation flow anymore (see module docstring,
        `topic_hint`); useful at call end as a cross-check against the model's own
        self-reported "missing_info" list."""
        for field_name in QUESTION_ORDER:
            if getattr(self, field_name) is None:
                return _TOPIC_HINTS_ES[field_name]
        return None

    def render_es(self) -> str:
        parts = []
        if self.pain_nrs is not None:
            parts.append(f"dolor {self.pain_nrs}/10")
        if self.fever_c is not None:
            parts.append(f"temperatura {self.fever_c}C")
        for field_name in ("mobility", "wound", "appetite", "sleep"):
            value = getattr(self, field_name)
            if value:
                parts.append(f"{_LABELS_ES[field_name]}: {value}")
        return ", ".join(parts) if parts else "(nada registrado aun)"

    @classmethod
    def from_db_row(cls, row: dict) -> "ClinicalSnapshot":
        return cls(
            pain_nrs=row.get("pain_nrs"),
            fever_c=float(row["fever_c"]) if row.get("fever_c") is not None else None,
            mobility=row.get("mobility"),
            wound=row.get("wound"),
            appetite=row.get("appetite"),
            sleep=row.get("sleep"),
        )
