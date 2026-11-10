"""Tucker — Cloud Change Actuator (the FINISHER / actuator).

auto_discover matches `cost_tuner_agent`. Tucker sits BEHIND the SRE/CTO gate
and the auto-apply safety floor. It never decides safety itself — it is the
concrete "@chain fails closed":

  a) authorisation RE-CHECK — reaching this agent does not mean the change is
     authorised. A change is applied ONLY on:
       · an explicit human approve TOKEN (allow-list), with a deny-list and an
         UNKNOWN/ambiguous verdict failing CLOSED; OR
       · a deterministically-floored SAFE tune (is_safe_tune true AND
         data_tier false — set by cost_anomaly's safety floor, which the LLM
         cannot waive).
  b) deterministic invariants a human can't waive — a recoverable proposed
     spec, and no double-apply (idempotency against the applied ledger).
  c) render the real applied-change record to ./out; on a tripped floor,
     DELETE the partial and record status=blocked with reasons — never a
     half-applied change.
  d) dev-store fallback that announces itself — records the apply in the store
     ledger (dev stand-in; no cloud API is called) and returns a rollback
     token; it NEVER fakes an ack when nothing was applied (status/applied say
     exactly what happened).

Two-layer defense: the BODY recovers + optimistically renders on its read; the
@chain floor re-derives authorisation INDEPENDENTLY and deletes the artifact if
it does not hold.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

from agency._shared import store
from agency._shared.session_stash import resolve_session_id, stash_get, stash_put

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")
_APPROVE_TOKENS = frozenset({"approve", "approved", "sign", "signed", "yes", "ok"})
_BLOCK_TOKENS = frozenset({"reject", "rejected", "no", "deny", "denied", "held",
                           "held_for_review", "hold", "halt_for_review",
                           "escalate_to_cto"})


def _signal(input_data: dict) -> str:
    """The human verdict, from wherever the SDK delivered it (shape-tolerant)."""
    sources = [input_data]
    up = input_data.get("upstream_yields")
    if isinstance(up, dict):
        sources.extend(v for v in up.values() if isinstance(v, dict))
    hd = input_data.get("human_data")
    if isinstance(hd, dict):
        sources.append(hd)
    for src in sources:
        for key in ("human_message", "human_decision", "decision", "message"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
    return ""


def _is_blocked(input_data: dict) -> bool:
    return _signal(input_data) in _BLOCK_TOKENS


def _lookup(key: str, *sources: Any) -> Any:
    """First non-empty value for key across dict sources (flat or one level
    deep, e.g. upstream_yields keyed by agent name)."""
    for src in sources:
        if not isinstance(src, dict):
            continue
        if src.get(key) not in (None, ""):
            return src[key]
        for v in src.values():
            if isinstance(v, dict) and v.get(key) not in (None, ""):
                return v[key]
    return None


def _recover_tune(input_data: dict, context: dict) -> dict:
    """Recover the tune across the HITL boundary: upstream -> human_data ->
    stash -> dev store. After a human gate the SDK re-delivers only the
    human-response meta, so the spec must come from the pre-gate stash."""
    sources = (
        input_data,
        input_data.get("upstream_yields"),
        input_data.get("human_data"),
        context.get("upstream") if isinstance(context, dict) else None,
    )
    spec = _lookup("proposed_spec", *sources)
    if not isinstance(spec, dict) or not spec:
        spec = _lookup("proposed_change", *sources)   # regulatory clean path
    tune = {
        "tune_id": str(_lookup("tune_id", *sources) or ""),
        "proposed_spec": spec if isinstance(spec, dict) else {},
        "is_safe_tune": bool(_lookup("is_safe_tune", *sources) or False),
        "data_tier": bool(_lookup("data_tier", *sources) or False),
    }
    if tune["proposed_spec"]:
        return tune
    # Nothing upstream — recover the whole floored payload from the stash.
    sid = resolve_session_id(input_data, context)
    stashed = stash_get(sid)
    if stashed.get("proposed_spec"):
        return {
            "tune_id": str(stashed.get("tune_id") or tune["tune_id"] or ""),
            "proposed_spec": stashed.get("proposed_spec") or {},
            "is_safe_tune": bool(stashed.get("is_safe_tune") or False),
            "data_tier": bool(stashed.get("data_tier") or False),
        }
    return tune


def _authorized(input_data: dict, tune: dict) -> bool:
    """Fail closed. Applied ONLY on an explicit approve token, OR a
    deterministically-floored safe non-data-tier tune. A block token, an
    unknown verdict, or an unsafe tune with no approval => NOT authorized."""
    sig = _signal(input_data)
    if sig in _BLOCK_TOKENS:
        return False
    if sig in _APPROVE_TOKENS:
        return True
    # No human token: the ONLY unattended apply is a floored-safe tune.
    return bool(tune.get("is_safe_tune")) and not bool(tune.get("data_tier"))


def _render_change_record(tune: dict, rollback_token: str, applied_at: str) -> str:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    tune_id = str(tune.get("tune_id") or "tune")
    path = _OUT_DIR / f"{tune_id}_applied_change.md"
    spec = tune.get("proposed_spec") or {}
    lines = [f"# Applied change record — {tune_id}",
             f"Applied at: {applied_at}",
             f"Rollback token: {rollback_token}",
             f"Safe (floored): {tune.get('is_safe_tune')}   Data-tier: {tune.get('data_tier')}",
             "", "## Proposed spec"]
    lines += [f"- {k}: {v}" for k, v in spec.items()] or ["- (empty)"]
    lines += ["", "_Dev: recorded in the applied-change ledger; no cloud API was called._"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _delete_partial(result: dict) -> None:
    p = result.get("artifact_path")
    if p:
        Path(p).unlink(missing_ok=True)
        result["artifact_path"] = ""


def enforce_apply_floor(result: Any, context: dict) -> dict:
    """The finisher floor — apply ONLY on a re-verified authorisation; delete
    the partial on a block. Re-derives authorisation INDEPENDENTLY of the body
    (never trusts the body's flag)."""
    result = result if isinstance(result, dict) else {}
    result.setdefault("block_reasons", [])
    tune = result.pop("_tune", {}) or {}
    inp = result.pop("_input", {}) or {}

    # Layer 2: independent re-check. Do not trust the body.
    if not _authorized(inp, tune):
        result.update(applied=False, status="blocked")
        if _is_blocked(inp):
            result["block_reasons"].append("approver rejected / held")
        elif tune.get("data_tier") or not tune.get("is_safe_tune"):
            result["block_reasons"].append(
                "unsafe / data-tier tune reached the actuator without approval (fail closed)")
        else:
            result["block_reasons"].append("no approval token on record (fail closed)")
        _delete_partial(result)
        logger.info(f"[tuner] apply floor BLOCKED tune={tune.get('tune_id') or '?'}: "
                    f"{result['block_reasons']}")
        return result

    spec = tune.get("proposed_spec") or {}
    if not spec:
        result.update(applied=False, status="blocked")
        result["block_reasons"].append(
            "no right-size spec recoverable after approval — nothing applied (approvals of "
            "drift, commitment or tag items are recorded only)")
        _delete_partial(result)
        logger.error(f"[tuner] REFUSING to apply — no spec recoverable "
                     f"(tune={tune.get('tune_id') or '?'}).")
        return result

    # Idempotency: a re-fired approval / duplicate webhook must not double-apply.
    tune_id = str(tune.get("tune_id") or "")
    if tune_id:
        prior = store.already_applied(tune_id)
        if prior:
            _delete_partial(result)   # the fresh render is a duplicate; drop it
            result.update(applied=True, status="noop",
                          applied_at_utc=str(prior.get("applied_at_utc") or ""),
                          rollback_token=str(prior.get("rollback_token") or ""))
            logger.info(f"[tuner] idempotent no-op — tune={tune_id} already applied")
            return result

    # Passed the floor — finalise the (already-rendered) apply + ledger it.
    now = datetime.now(timezone.utc).isoformat()
    rollback = str(result.get("rollback_token") or f"rb-{uuid.uuid4().hex[:10]}")
    if not result.get("artifact_path"):
        result["artifact_path"] = _render_change_record(tune, rollback, now)
    store.record_applied({"tune_id": tune_id, "rollback_token": rollback,
                          "applied_at_utc": now})
    result.update(applied=True, status="applied", applied_at_utc=now,
                  rollback_token=rollback)
    logger.info(f"[tuner] applied tune={tune_id or '?'} spec={list(spec.keys())} "
                f"rollback={rollback} -> {result['artifact_path']}")
    return result


@chain(enforce_apply_floor)
async def cost_tuner_agent(llm_response: Any, input_data: Any, context: Any) -> dict:
    """Recover the tune, optimistically render on the body's read, and hand the
    floor an independent re-check. The floor is the authority."""
    input_data = input_data if isinstance(input_data, dict) else {}
    context = context if isinstance(context, dict) else {}
    tune = _recover_tune(input_data, context)
    sid = resolve_session_id(input_data, context)
    if tune.get("proposed_spec"):
        stash_put(sid, tune)   # keep it recoverable across a retry

    out: dict[str, Any] = {
        "applied": False, "applied_at_utc": "", "rollback_token": "",
        "tune_id": tune.get("tune_id", ""), "status": "", "block_reasons": [],
        "artifact_path": "",
        "_tune": tune, "_input": input_data,
    }

    # Optimistic render on the body's read (Layer 1). The floor deletes it if
    # its INDEPENDENT re-check does not authorise the apply.
    if _authorized(input_data, tune) and tune.get("proposed_spec"):
        rollback = f"rb-{uuid.uuid4().hex[:10]}"
        out["rollback_token"] = rollback
        out["artifact_path"] = _render_change_record(
            tune, rollback, datetime.now(timezone.utc).isoformat())
    return out
