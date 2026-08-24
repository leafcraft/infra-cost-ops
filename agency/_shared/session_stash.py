"""Per-session invoice stash — bridges the HITL gap.

When a human approves in the Studio inbox, the SDK resumes the chain
with ONLY the human-response meta fields (human_message, human_decision,
…) — the invoice payload that was flowing through the chain is not
re-delivered to the next agent. The approval_router stashes the full
merged invoice context here, keyed by session_id, so erp_poster (and
the audit sink) can recover it after the human gate.

In-process only — survives the human wait because the mesh is one
process. A multi-replica deployment swaps this for the Redis session
store; the get/put interface stays the same.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

_LOCK = threading.Lock()
_MAX_SESSIONS = 500
_STASH: OrderedDict[str, dict[str, Any]] = OrderedDict()


def stash_put(session_id: str, payload: dict[str, Any]) -> None:
    if not session_id:
        return
    with _LOCK:
        # Preserve the per-session bounded-loop counters across a payload
        # overwrite — they are session-scoped, independent of the payload.
        prior = _STASH.get(session_id) or {}
        rec = dict(payload)
        if "_counters" in prior:
            rec["_counters"] = prior["_counters"]
        _STASH[session_id] = rec
        _STASH.move_to_end(session_id)
        while len(_STASH) > _MAX_SESSIONS:
            _STASH.popitem(last=False)


def stash_get(session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    with _LOCK:
        return dict(_STASH.get(session_id) or {})


def bump(session_id: str, key: str, by: int = 1) -> int:
    """Increment a per-session counter and return the NEW value. Used to
    BOUND a back-edge (rework / replan loop) — the caller escalates at the
    cap. In-process only; a multi-replica deploy swaps this for Redis."""
    if not session_id:
        return 0
    with _LOCK:
        rec = _STASH.get(session_id)
        if rec is None:
            rec = {}
            _STASH[session_id] = rec
        counters = rec.setdefault("_counters", {})
        counters[key] = int(counters.get(key, 0)) + int(by)
        _STASH.move_to_end(session_id)
        while len(_STASH) > _MAX_SESSIONS:
            _STASH.popitem(last=False)
        return counters[key]


def peek(session_id: str, key: str, default: int = 0) -> int:
    """Read a per-session counter without incrementing it."""
    if not session_id:
        return default
    with _LOCK:
        rec = _STASH.get(session_id) or {}
        return int((rec.get("_counters") or {}).get(key, default))


def resolve_session_id(input_data: dict[str, Any], context: dict[str, Any]) -> str:
    for src in (input_data, context):
        if not isinstance(src, dict):
            continue
        for key in ("session_id", "sessionId"):
            sid = src.get(key)
            if sid:
                return str(sid)
        owd = src.get("original_webhook_data")
        if isinstance(owd, dict) and owd.get("session_id"):
            return str(owd["session_id"])
    return ""
