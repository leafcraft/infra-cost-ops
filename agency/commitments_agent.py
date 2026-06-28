"""commitments_agent — SELF-RELIANT commitments analyst + savings floor.

The LLM (configs/config.yaml) writes the utilisation review + the
rebalance narrative; this module owns the three things Python must:

@pre_compose  load_commitments — pull the RI + Savings-Plan inventory
              (expected vs actual utilisation, on-demand burn, expiry)
              from the source store (AWS RI/SP, GCP CUDs in prod) so the
              analyst reasons over the REAL commitment picture instead of
              a spoon-fed payload. Self-reliant context pull.

@chain        enforce_savings_floor — the DETERMINISTIC SAVINGS math the
              LLM can't be trusted to total. It recomputes
              total_wasted_cents_per_day from the under-used commitments
              in the inventory (expected_use - actual_use over the
              on-demand monthly cost), and FAIL-CLOSED clamps every
              savings total: a turn with no demonstrable waste routes to
              the audit log, never to the SRE. The LLM may RAISE a flagged
              waste but never invent savings the inventory doesn't support.

@compose      per-downstream payloads — the two callees need different
              things: sre_human gets the full forecast + rebalance set to
              defend the recommendation; the audit logger gets only the
              zero-savings terminus summary.

Everything else (the executive_summary, the rebalance rationale) stays
prompt-driven and SDK yields-filled.
"""
from __future__ import annotations

import json
from typing import Any

from leafmesh import LeafMeshLogger, chain, compose, pre_compose

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


# ── @pre_compose — pull the commitment inventory the analyst reviews ──
def load_commitments(data: Any, ctx: Any) -> dict[str, Any]:
    """Fetch the RI + Savings-Plan inventory. PROD: AWS RI/SP, GCP CUDs."""
    inv = store.commitment_inventory()
    ris = inv.get("reserved_instances") or []
    sps = inv.get("savings_plans") or []
    logger.info(
        f"[commitments] pulled inventory: {len(ris)} RIs, {len(sps)} SPs "
        f"(as_of={inv.get('as_of_utc')})"
    )
    return {
        "commitment_inventory": inv,
        "reserved_instances": ris,
        "savings_plans": sps,
    }


def _ctx(context: Any) -> dict[str, Any]:
    prepared = (context or {}).get("prepared_data", {}) if isinstance(context, dict) else {}
    c = prepared.get("context", {}) if isinstance(prepared, dict) else {}
    return c if isinstance(c, dict) else {}


def compute_waste(inventory: dict[str, Any]) -> dict[str, Any]:
    """Deterministic waste math over the under-used commitments: for each
    commit whose actual utilisation trails its expected, the wasted spend
    is (expected - actual) × on-demand monthly cost, prorated daily. Pure;
    testable directly."""
    inventory = inventory or {}
    underused: list[dict[str, Any]] = []
    total_daily = 0
    for ri in (inventory.get("reserved_instances") or []):
        exp = float(ri.get("expected_use_pct") or 0)
        act = float(ri.get("actual_use_pct") or 0)
        if act < exp:
            monthly = int(ri.get("on_demand_monthly_cents") or 0)
            wasted_daily = int(round((exp - act) * monthly / 30.0))
            total_daily += wasted_daily
            underused.append({
                "commit_id": ri.get("commit_id"), "type": ri.get("type"),
                "expected_use_pct": exp, "actual_use_pct": act,
                "wasted_cents_per_day": wasted_daily,
            })
    for sp in (inventory.get("savings_plans") or []):
        exp = float(sp.get("expected_use_pct") or 0)
        act = float(sp.get("actual_use_pct") or 0)
        if act < exp:
            underused.append({
                "commit_id": sp.get("commit_id"), "type": sp.get("type"),
                "expected_use_pct": exp, "actual_use_pct": act,
                "wasted_cents_per_day": 0,    # SP waste is hourly-rate based; flagged only
            })
    return {
        "underused_commits": underused,
        "total_wasted_cents_per_day": total_daily,
        "computed_monthly_savings_cents": int(round(total_daily * 30.0)),
    }


# ── The savings floor as a PURE helper (unit-testable) ────────────────
def apply_savings_floor(result: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Recompute the waste totals from the inventory and FAIL-CLOSED clamp:
    no demonstrable waste AND no forecast savings → every total ZERO so the
    turn routes to audit, not the SRE. The LLM may raise flagged waste but
    not invent savings the inventory can't support."""
    result = dict(result) if isinstance(result, dict) else {}
    waste = compute_waste(inventory)

    # Floor: the wasted total is the COMPUTED total — the LLM doesn't get
    # to under- or over-state it. Keep whichever underused list is richer.
    result["total_wasted_cents_per_day"] = waste["total_wasted_cents_per_day"]
    if not result.get("underused_commits"):
        result["underused_commits"] = waste["underused_commits"]

    # Coerce the contract numerics to ints / clamp savings_pct to 0..1.
    try:
        monthly = int(result.get("total_potential_savings_monthly_cents") or 0)
    except (TypeError, ValueError):
        monthly = 0
    try:
        pct = float(result.get("expected_savings_pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(1.0, pct))
    result["expected_savings_pct"] = pct
    term = result.get("term_months")
    result["term_months"] = term if term in (12, 36) else 12

    # SAVINGS FLOOR — the analyst may not invent savings the inventory can't
    # support. A reserved-capacity FORECAST (recommended_commit OR a
    # expected_savings_pct > 0 — the ri_expiry / savings_plan_review entries)
    # is a real signal that carries its own number; absent a forecast, the
    # monthly potential is CLAMPED DOWN to the computed inventory basis.
    forecast_override = bool(result.get("recommended_commit")) or pct > 0
    computed = waste["computed_monthly_savings_cents"]
    if forecast_override:
        monthly = max(monthly, computed)
    else:
        if monthly > computed:
            logger.warning(
                f"[commitments] SAVINGS FLOOR clamped LLM potential "
                f"{monthly}c → inventory basis {computed}c (no forecast signal)"
            )
        monthly = computed
    result["total_potential_savings_monthly_cents"] = monthly

    # FAIL-CLOSED: no waste + no forecast savings → zero everything so the
    # condition routes this turn to the audit log, never the SRE.
    if monthly == 0 and result["total_wasted_cents_per_day"] == 0 \
            and result["expected_savings_pct"] == 0:
        result["rebalance_recommendations"] = result.get("rebalance_recommendations") or []
        logger.info("[commitments] fail-closed: zero savings → audit terminus")
    return result


async def enforce_savings_floor(result: Any, ctx: Any) -> Any:
    """@chain wrapper — recompute waste from the store inventory. The body
    attaches the inventory as a private `_savings_inventory` carry key."""
    result = _parse(result)
    inventory = result.pop("_savings_inventory", None)
    if not inventory:
        inventory = store.commitment_inventory()
    return apply_savings_floor(result, inventory)


# ── @compose — per-downstream payloads (callees need different data) ──
def _to_sre(result: dict, ctx: Any) -> dict[str, Any]:
    """The SRE defends the rebalance — give the full forecast + the
    rebalance set + the executive summary + the tune identity."""
    return {
        "tune_id": result.get("tune_id", ""),
        "underused_commits": result.get("underused_commits", []),
        "overused_workloads": result.get("overused_workloads", []),
        "rebalance_recommendations": result.get("rebalance_recommendations", []),
        "total_potential_savings_monthly_cents":
            result.get("total_potential_savings_monthly_cents", 0),
        "expected_savings_pct": result.get("expected_savings_pct", 0),
        "term_months": result.get("term_months", 12),
        "executive_summary": result.get("executive_summary", ""),
    }


def _to_audit(result: dict, ctx: Any) -> dict[str, Any]:
    """The zero-savings terminus — the audit logger only needs the summary
    facts, not the full forecast object."""
    return {
        "payload": {
            "kind": "commitments_review",
            "total_wasted_cents_per_day": result.get("total_wasted_cents_per_day", 0),
            "total_potential_savings_monthly_cents":
                result.get("total_potential_savings_monthly_cents", 0),
            "executive_summary": result.get("executive_summary", ""),
        },
    }


@pre_compose(context_processor=load_commitments)
@compose(
    sre_human=_to_sre,
    infra_audit_logger_agent=_to_audit,
)
@chain(enforce_savings_floor)
async def commitments_agent(llm_response: Any, input_data: Any, context: Any) -> Any:
    """Surface the LLM's utilisation review; the chain enforces the savings
    floor. The body resolves the inventory (it has context) and hands it to
    the floor via a private carry key."""
    result = _parse(llm_response)
    pctx = _ctx(context)
    inventory = pctx.get("commitment_inventory") or store.commitment_inventory()
    result["_savings_inventory"] = inventory
    return result
