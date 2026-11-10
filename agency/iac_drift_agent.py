"""iac_drift_agent — SELF-RELIANT drift reconciler + blast-radius floor.

The LLM (defined in the configuration) writes the drift narrative; this module owns
the two things Python must:

@pre_compose  load_drift_state — pull the last `terraform plan` / Pulumi
              preview drift set from the source store (the IaC planner in
              prod) so the reconciler reasons over the REAL drift items,
              not a spoon-fed payload. Self-reliant context pull.

@chain        enforce_blast_radius — THE DETERMINISTIC CRITICAL-DRIFT
              FLOOR. A critical-resource drift (auth / billing / security /
              kms / iam / secrets …) is a security event, so the
              escalation is reproducible keyword arithmetic the LLM may NOT
              waive (first principle 6). It re-scans every drifted resource
              + attribute against the critical-keyword taxonomy and FORCES
              critical_drift=true on any hit, regardless of what the LLM
              scored. The floor only ever RAISES criticality — the LLM can
              never clear a kms/iam drift to "clean".

Everything else (the drift_summary narrative) stays prompt-driven and SDK
yields-filled.
"""
from __future__ import annotations

import json
from typing import Any

from leafmesh import LeafMeshLogger, chain, pre_compose

from agency._shared import store

logger = LeafMeshLogger(__name__)


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


# ── @pre_compose — pull the drift set the reconciler reviews ──────────
def load_drift_state(data: Any, ctx: Any) -> dict[str, Any]:
    """Fetch the last plan's drift items. PROD: the terraform/pulumi
    planner. DEV: the seeded store — the input payload's drifted_attributes
    take precedence when present (a real plan came in on the webhook)."""
    input_data = data if isinstance(data, dict) else {}
    payload_items = _pick(input_data, "drifted_attributes", None) \
        or _pick(input_data, "drift_items", None)
    if payload_items:
        items = [{"attribute": str(a)} for a in payload_items] \
            if isinstance(payload_items, list) else []
    else:
        items = list(store.drift_state().get("items") or [])
    logger.info(f"[iac_drift] loaded {len(items)} drift item(s)")
    return {"drift_items": items}


def _ctx(context: Any) -> dict[str, Any]:
    prepared = (context or {}).get("prepared_data", {}) if isinstance(context, dict) else {}
    c = prepared.get("context", {}) if isinstance(prepared, dict) else {}
    return c if isinstance(c, dict) else {}


def _item_text(item: Any) -> str:
    """Flatten one drift item to a searchable blob (resource + attribute)."""
    if isinstance(item, dict):
        return " ".join(str(v) for v in item.values()).lower()
    return str(item).lower()


# ── The blast-radius floor as a PURE helper (unit-testable) ───────────
def apply_blast_radius_floor(result: dict[str, Any],
                             drift_items: list[Any]) -> dict[str, Any]:
    """Re-scan the drift items against the critical-keyword taxonomy and
    FORCE critical_drift on any hit. The LLM can never CLEAR a critical
    drift. Pure — the caller supplies the items (read from store/payload)."""
    result = dict(result) if isinstance(result, dict) else {}
    items = list(drift_items or [])

    hits: list[str] = []
    for it in items:
        blob = _item_text(it)
        matched = [k for k in store.CRITICAL_KEYWORDS if k in blob]
        if matched:
            hits.extend(matched)

    result["drift_count"] = len(items)
    if not result.get("drift_summary"):
        result["drift_summary"] = items

    llm_critical = bool(result.get("critical_drift"))
    deterministic_critical = bool(hits)
    result["critical_drift"] = llm_critical or deterministic_critical
    result["critical_resources"] = sorted(set(hits))

    if deterministic_critical and not llm_critical:
        logger.warning(
            f"[iac_drift] BLAST-RADIUS FLOOR forced critical_drift=true "
            f"({sorted(set(hits))}) — the LLM scored a critical-resource "
            f"drift as clean; routing to SRE."
        )
    return result


async def enforce_blast_radius(result: Any, ctx: Any) -> Any:
    """@chain wrapper — re-scan the drift items from the store/carry. The
    body attaches the resolved items as a private `_drift_items` carry key
    so this fires identically through the runtime and on a direct call."""
    result = _parse(result)
    items = result.pop("_drift_items", None)
    if items is None:
        items = result.get("drift_summary") or list(store.drift_state().get("items") or [])
    return apply_blast_radius_floor(result, items)


@pre_compose(context_processor=load_drift_state)
@chain(enforce_blast_radius)
async def iac_drift_agent(llm_response: Any, input_data: Any, context: Any) -> Any:
    """Surface the LLM's drift narrative; the chain enforces the
    blast-radius floor. The body resolves the drift items (via @pre_compose
    or the store) and hands them to the floor via a private carry key."""
    result = _parse(llm_response)
    pctx = _ctx(context)
    items = pctx.get("drift_items")
    if items is None:
        items = result.get("drift_summary") or list(store.drift_state().get("items") or [])
    result["_drift_items"] = items
    return result
