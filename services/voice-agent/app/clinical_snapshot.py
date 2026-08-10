"""The six-signal running clinical picture for a call, built up incrementally as Track B
classifies each turn -- pain_nrs/fever_c/mobility/wound/appetite/sleep, the exact
taxonomy the reference dataset's trajectories use (docs/dataset-eda.md §3, §7). This is
what makes "keep all the relevant patient context for the agent at any point in time" a
real property of the system rather than a turn-by-turn amnesia: the agent injects its
own current snapshot back into context on every subsequent turn (app/main.py's
on_user_turn_completed) so it doesn't re-ask something it already knows, and the
snapshot is upserted to Postgres after every turn (app/db.py's upsert_clinical_snapshot),
not just once at call end, so it survives a reconnect (plan §2.10) or an agent process
restart mid-call.

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
        if value in valid:
            setattr(self, field, value)
        else:
            logger.warning("dropping out-of-vocabulary %s from model: %r", field, value)

    def has_any(self) -> bool:
        return any(getattr(self, f.name) is not None for f in fields(self))

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
