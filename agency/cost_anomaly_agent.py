"""cost_anomaly_agent — Axel, the cost analyst + the SAFETY GATE floor.

The LLM (configs/config.yaml, wrapped in super_agent) classifies the anomaly
+ drafts the right-sizing tune, reading the telemetry through its tools
(read_telemetry / read_blast_radius — dev store day-0). This module owns the
things Python must:

@chain        enforce_safety_floor — THE DETERMINISTIC AUTO-APPLY GATE.
              The auto-apply-safe vs risky/data-tier → SRE decision is
              irreversible cloud action, so it is reproducible arithmetic
              the LLM may NOT waive (first principle 6). It re-derives:
                · data_tier  — from the resource KIND (databases/storage),
                  not from what the LLM scored;
                · blast_radius — the downstream fan-out + whether it
                  touches a critical resource (auth/billing/security/
                  kms/iam), from the CMDB;
                · is_safe_tune — forced FALSE on any data-tier resource,
                  any new_resource, OR a blast radius above the auto cap /
                  touching a critical resource.
              It also recomputes proposed_spec + daily_savings_cents from
              the pricing ladder when the LLM left them empty (the right-
              sizing math). The floor only ever DOWNGRADES safety — the
              LLM can never upgrade an unsafe tune to auto-apply.

@chain        stash_tune — stash the (floored) tune payload keyed by
              session BEFORE the sre_human gate. The SDK resumes a HITL
              approval with only the human-response meta fields, so the
              tune payload must be stashed here for cost_tuner to recover.

Bounded back-edge: when the SRE REJECTS a tune, sre_human routes back here
for a more conservative re-proposal. The body bumps a per-session counter and
trips `reproposal_exhausted` at the cap, so the re-propose loop terminates
into the WORM audit instead of looping forever (Rule 3).

Everything else (classification narrative, sre_briefing, cited_signals)
stays prompt-driven and SDK yields-filled.
"""
from __future__ import annotations

import json
from typing import Any

from leafmesh import LeafMeshLogger, chain

from agency._shared import store
from agency._shared.session_stash import bump, resolve_session_id, stash_get, stash_put

logger = LeafMeshLogger(__name__)

# Max SRE rejects before the re-propose loop terminates into the audit sink.
_REPROPOSAL_CAP = 2

_REJECT_TOKENS = frozenset({"reject", "rejected"})


def _is_reject_reentry(input_data: dict[str, Any]) -> bool:
    """True when this turn is a re-entry from an SRE rejection (bounded back-edge)."""
    if not isinstance(input_data, dict):
        return False
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
            if isinstance(val, str) and val.strip().lower() in _REJECT_TOKENS:
                return True
    return False


def _parse(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except (ValueError, TypeError):
            return {}
    return {}


def _pick(data: dict[str, Any], key: str, default: Any = None) -> Any:
    """Flat input_data, then payload, then upstream_yields (flat / nested)."""
    if not isinstance(data, dict):
        return default
    if data.get(key) not in (None, ""):
        return data[key]
    payload = data.get("payload")
    if isinstance(payload, dict) and payload.get(key) not in (None, ""):
        return payload[key]
    uy = data.get("upstream_yields")
    if isinstance(uy, dict):
        if uy.get(key) not in (None, ""):
            return uy[key]
        for v in uy.values():
            if isinstance(v, dict) and v.get(key) not in (None, ""):
                return v[key]
    return default


# ── @pre_compose — pull the telemetry + tags the analyst reasons over ──
def load_telemetry(data: Any, ctx: Any) -> dict[str, Any]:
    """Fetch the anomaly's cost/usage detail + the resource tags / blast
    radius. PROD: Cost Explorer + CloudWatch + the CMDB. DEV: the seeded
    store. The webhook only carries an id; the detail is pulled here."""
    input_data = data if isinstance(data, dict) else {}
    tune_id = str(_pick(input_data, "tune_id", "") or "")
    tel = store.telemetry(tune_id)
    resource_id = str(_pick(input_data, "resource_id", "")
                      or tel.get("resource_id") or "")
    blast = store.blast_radius(resource_id) if resource_id else {}
    logger.info(
        f"[cost_anomaly] pulled telemetry for tune={tune_id or '?'} "
        f"resource={resource_id or '?'} (blast={blast.get('downstream_count', 0)} "
        f"critical={blast.get('touches_critical', False)})"
    )
    return {
        "telemetry": tel,
        "resource_id": resource_id,
        "blast_radius": blast,
    }


def _ctx(context: Any) -> dict[str, Any]:
    prepared = (context or {}).get("prepared_data", {}) if isinstance(context, dict) else {}
    c = prepared.get("context", {}) if isinstance(prepared, dict) else {}
    return c if isinstance(c, dict) else {}


# ── The safety floor as a PURE helper (unit-testable, no I/O) ─────────
def apply_safety_floor(result: dict[str, Any],
                       resource_kind: str,
                       resource_id: str,
                       blast: dict[str, Any]) -> dict[str, Any]:
    """Re-derive data_tier / blast_radius / is_safe_tune deterministically.
    The LLM may never UPGRADE an unsafe tune to auto-apply. Pure — the
    caller supplies the kind/id/blast (read from the store)."""
    result = dict(result) if isinstance(result, dict) else {}
    blast = blast or {}

    # data_tier — from the resource KIND, overriding the LLM.
    data_tier = store.is_data_tier(resource_kind, resource_id)
    result["data_tier"] = bool(data_tier)

    # blast_radius — surfaced for the SRE briefing + the gate.
    downstream_count = int(blast.get("downstream_count") or 0)
    touches_critical = bool(blast.get("touches_critical"))
    result["blast_radius"] = downstream_count
    result["touches_critical"] = touches_critical

    anomaly_type = str(result.get("anomaly_type") or "").strip().lower()
    over_cap = downstream_count > store.BLAST_RADIUS_AUTO_CAP

    # is_safe_tune — forced FALSE on any unsafe condition. Starts from the
    # LLM's claim but can only ever be DOWNGRADED here.
    llm_safe = bool(result.get("is_safe_tune"))
    forced_unsafe = (
        data_tier
        or anomaly_type == "new_resource"
        or touches_critical
        or over_cap
    )
    result["is_safe_tune"] = llm_safe and not forced_unsafe

    if forced_unsafe and llm_safe:
        reasons = []
        if data_tier:
            reasons.append("data_tier")
        if anomaly_type == "new_resource":
            reasons.append("new_resource")
        if touches_critical:
            reasons.append("critical_resource")
        if over_cap:
            reasons.append(f"blast_radius>{store.BLAST_RADIUS_AUTO_CAP}")
        logger.warning(
            f"[cost_anomaly] SAFETY FLOOR forced is_safe_tune=false "
            f"(tune={result.get('tune_id') or '?'}; reasons={reasons}) — "
            f"the LLM scored it safe but it must go to SRE review."
        )
    return result


def fill_rightsizing(result: dict[str, Any], tel: dict[str, Any]) -> dict[str, Any]:
    """Recompute proposed_spec + daily_savings_cents from the pricing
    ladder when the LLM left them empty — the right-sizing math the LLM
    can't reliably do. Only fills gaps; never overwrites a present value."""
    result = dict(result)
    current = result.get("current_spec") or tel.get("current_spec") or {}
    if current and not result.get("current_spec"):
        result["current_spec"] = current
    proposed = result.get("proposed_spec")
    if (not proposed) and current and result.get("is_safe_tune"):
        calc = store.rightsizing_savings(current, int(tel.get("monthly_cost_cents") or 0))
        if calc:
            result["proposed_spec"] = calc["proposed_spec"]
            if not result.get("daily_savings_cents"):
                result["daily_savings_cents"] = calc["daily_savings_cents"]
            logger.info(
                f"[cost_anomaly] right-sizing math filled proposed_spec="
                f"{calc['proposed_spec']} daily_savings={calc['daily_savings_cents']}c"
            )
    return result


async def enforce_safety_floor(result: Any, ctx: Any) -> Any:
    """@chain wrapper — re-derive safety from the store, then fill the
    right-sizing math. The body attaches the resolved kind/id/blast as
    private `_floor_*` carry keys so this fires identically through the
    SDK runtime and on a direct call."""
    result = _parse(result)
    resource_kind = str(result.pop("_floor_kind", "") or "")
    resource_id = str(result.pop("_floor_resource_id", "") or "")
    blast = result.pop("_floor_blast", None) or {}
    tel = result.pop("_floor_telemetry", None) or {}
    if not blast and resource_id:
        blast = store.blast_radius(resource_id)
    if not tel:
        tel = store.telemetry(str(result.get("tune_id") or ""))
    floored = apply_safety_floor(result, resource_kind, resource_id, blast)
    return fill_rightsizing(floored, tel)


# ── @chain — stash the floored tune payload before the SRE gate ───────
async def stash_tune(result: Any, ctx: Any) -> Any:
    result = _parse(result)
    # The body resolved the session_id (it has input_data) and carried it
    # here as a private key — @chain fns don't receive input_data, and on a
    # direct call ctx is empty. Strip it after reading.
    session_id = str(result.pop("_floor_session_id", "") or "") \
        or resolve_session_id(result, ctx if isinstance(ctx, dict) else {})
    stash_put(session_id, {
        "tune_id": result.get("tune_id") or "",
        "current_spec": result.get("current_spec") or {},
        "proposed_spec": result.get("proposed_spec") or {},
        "daily_savings_cents": result.get("daily_savings_cents") or 0,
        "data_tier": result.get("data_tier") or False,
        "is_safe_tune": result.get("is_safe_tune") or False,
        "blast_radius": result.get("blast_radius") or 0,
    })
    logger.info(
        f"[cost_anomaly] stashed tune={result.get('tune_id') or '?'} "
        f"(session={session_id or '?'} safe={result.get('is_safe_tune')})"
    )
    return result


@chain(enforce_safety_floor, stash_tune)
async def cost_anomaly_agent(llm_response: Any, input_data: Any, context: Any) -> Any:
    """Surface the LLM's classification; the chain enforces the safety floor +
    stashes for HITL recovery. The body resolves the resource kind/id + blast
    from the store (the model already read them via its tools; the floor
    re-reads independently) and bounds the SRE-reject re-proposal loop."""
    result = _parse(llm_response)
    input_data = input_data if isinstance(input_data, dict) else {}
    session_id = resolve_session_id(input_data, context)

    # tune_id — from the LLM / input / upstream, else recover from the stash
    # (an SRE-reject re-entry re-delivers only human meta; the tune_id it
    # needs to re-read telemetry lives in the pre-gate stash).
    tune_id = str(result.get("tune_id") or _pick(input_data, "tune_id", "") or "")
    if not tune_id:
        tune_id = str((stash_get(session_id) or {}).get("tune_id") or "")
    result["tune_id"] = tune_id

    # Resolve the telemetry / resource / blast from the store so the floor is
    # self-sufficient (the model pulled these via its tools for the narrative;
    # the deterministic floor re-derives them here — A7: verify independently).
    tel = store.telemetry(tune_id)
    resource_id = str(_pick(input_data, "resource_id", "") or tel.get("resource_id") or "")
    blast = store.blast_radius(resource_id) if resource_id else {}
    resource_kind = str(_pick(input_data, "resource_kind", "") or tel.get("resource_kind") or "")

    # Bounded back-edge: count SRE rejections; trip the terminal flag at the cap.
    if _is_reject_reentry(input_data):
        count = bump(session_id, "reproposal_count")
        result["reproposal_exhausted"] = count > _REPROPOSAL_CAP
        if result["reproposal_exhausted"]:
            logger.warning(f"[cost_anomaly] re-proposal EXHAUSTED for tune={tune_id or '?'} "
                           f"(session={session_id or '?'}, rejects={count}) — routing to audit.")
    else:
        result.setdefault("reproposal_exhausted", False)

    # Private carry → consumed + stripped by enforce_safety_floor / stash_tune.
    result["_floor_kind"] = resource_kind
    result["_floor_resource_id"] = resource_id
    result["_floor_blast"] = blast
    result["_floor_telemetry"] = tel
    result["_floor_session_id"] = session_id
    return result
