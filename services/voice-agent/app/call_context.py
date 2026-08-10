"""CallContext: everything voice-agent knows about who it's talking to, sourced from
the LiveKit room metadata api-gateway attaches at room creation
(services/api-gateway/internal/httpapi/calls.go's roomMetadata) -- closes the
"voice-agent has no way to know which patient/category a call is for" gap
(specs/implementation-plan.md §2.1, §2.10). A room with no metadata (an anonymous call,
or one created before this existed) still works -- every field here is optional and the
agent degrades to the same behavior as before patients existed.
"""

import json
from dataclasses import dataclass


@dataclass
class CallContext:
    call_id: str
    patient_id: str | None = None
    patient_name: str | None = None
    category: str | None = None
    procedure: str | None = None
    postop_day: int | None = None


def from_room(room_name: str, metadata_json: str | None) -> CallContext:
    call_id = room_name.removeprefix("call-")

    if not metadata_json:
        return CallContext(call_id=call_id)

    try:
        meta = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return CallContext(call_id=call_id)

    return CallContext(
        call_id=call_id,
        patient_id=meta.get("patient_id") or None,
        patient_name=meta.get("patient_name") or None,
        category=meta.get("category") or None,
        procedure=meta.get("procedure") or None,
        postop_day=meta.get("postop_day"),
    )
