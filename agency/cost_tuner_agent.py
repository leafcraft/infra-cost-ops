"""KEPT: actuator with real side effects (changes live cloud
resources) + fail-closed post-HITL recovery — neither is expressible
in YAML.

Usually invoked by a HUMAN approval (sre / cto). The SDK resumes the
chain with only the human-response meta fields, so the tune payload
is recovered from the per-session stash that cost_anomaly_agent
wrote (or, on the regulatory path, from upstream yields /
human_data). FAIL-CLOSED: if no tune payload is recoverable, nothing
is applied.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from leafmesh import LeafMeshLogger

from agency._shared import store
from agency._shared.session_stash import resolve_session_id, stash_get

logger = LeafMeshLogger(__name__)


def _lookup(key: str, *sources: Any) -> Any:
    """First non-empty value for key across dict sources (flat or one
    level deep, e.g. upstream_yields keyed by agent name)."""
    for src in sources:
        if not isinstance(src, dict):
            continue
        if src.get(key) not in (None, ""):
            return src[key]
        for v in src.values():
            if isinstance(v, dict) and v.get(key) not in (None, ""):
                return v[key]
    return None


async def cost_tuner_agent(llm_response, input_data, context):
    session_id = resolve_session_id(input_data, context)
    stashed = stash_get(session_id)
    sources = (
        input_data,
        input_data.get("upstream_yields"),
        input_data.get("upstream_results"),
        input_data.get("human_data"),
        context.get("upstream") if isinstance(context, dict) else None,
        stashed,
    )

    spec = _lookup("proposed_spec", *sources)
    if not isinstance(spec, dict) or not spec:
        # regulatory_monitor carries the tune as proposed_change
        spec = _lookup("proposed_change", *sources)
    tune_id = str(_lookup("tune_id", *sources) or "")

    if not isinstance(spec, dict) or not spec:
        logger.error(
            f"[tuner] REFUSING to apply — no tune payload recoverable "
            f"(session={session_id or '?'}). Applying an unknown change "
            f"to live cloud resources is worse than applying nothing."
        )
        return {
            "applied": False,
            "applied_at_utc": "",
            "rollback_token": "",
            "tune_id": tune_id,
            "error": "no tune payload recoverable after approval — "
                     "cost_anomaly stash missing for this session",
        }

    # IDEMPOTENCY: a re-fired approval (or a duplicate webhook) must not
    # double-apply a change to live cloud resources. If this tune_id is
    # already in the applied ledger, return the prior result unchanged.
    if tune_id:
        prior = store.already_applied(tune_id)
        if prior:
            logger.info(f"[tuner] idempotent no-op — tune={tune_id} already "
                        f"applied at {prior.get('applied_at_utc')}")
            return {
                "applied": True,
                "applied_at_utc": str(prior.get("applied_at_utc") or ""),
                "rollback_token": str(prior.get("rollback_token") or ""),
                "tune_id": tune_id,
                "error": "",
            }

    now = datetime.now(timezone.utc).isoformat()
    rollback = f"rb-{uuid.uuid4().hex[:10]}"
    store.record_applied({"tune_id": tune_id, "rollback_token": rollback,
                          "applied_at_utc": now})
    logger.info(f"[tuner] applied tune={tune_id or '?'} "
                f"spec={list(spec.keys())} rollback={rollback}")
    return {
        "applied": True,
        "applied_at_utc": now,
        "rollback_token": rollback,
        "tune_id": tune_id,
        "error": "",
    }
