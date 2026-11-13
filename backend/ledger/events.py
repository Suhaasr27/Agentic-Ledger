"""Event line parsing (pure). One JSON line in, a ``ParsedEvent`` or ``None`` (malformed) out.

Only *structural* checks happen here (valid JSON object, has an event_id, parseable timestamp).
Duplicate / orphan / unknown-disposition decisions need ledger state and are derived in SQL.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .normalize import normalize_phone, parse_timestamp


@dataclass(frozen=True)
class ParsedEvent:
    event_id: str
    workflow_id: str
    event_type: str
    call_id: str | None
    occurred_at: datetime
    phone_hash: bytes | None  # None when the event carries no (valid) phone, e.g. overrides
    payload: dict


def parse_event_line(line: str, phone_hasher: Callable[[str], bytes]) -> ParsedEvent | None:
    """``None`` means malformed: not JSON, not an object, no event_id, no event_type or bad timestamp.

    The raw phone is hashed here and never leaves this function, so no plaintext phone can reach the DB.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    event_id = data.get("event_id")
    if event_id is None or str(event_id).strip() == "":
        return None
    event_type = data.get("event_type")
    occurred_at = parse_timestamp(data.get("occurred_at"))
    if not isinstance(event_type, str) or not event_type or occurred_at is None:
        return None

    canonical = normalize_phone(data.get("phone"))
    call_id = data.get("call_id")
    payload = data.get("payload")
    return ParsedEvent(
        event_id=str(event_id).strip(),
        workflow_id=str(data.get("workflow_id") or ""),
        event_type=event_type,
        call_id=str(call_id) if call_id not in (None, "") else None,
        occurred_at=occurred_at,
        phone_hash=phone_hasher(canonical) if canonical else None,
        payload=payload if isinstance(payload, dict) else {},
    )
