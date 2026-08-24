"""finops_governance_agent — Gwen, the FinOps governance analyst (fanned out).

Gwen is an `llm` agent run with `instances: N`: each activation fans into N
parallel copies that each claim a DISJOINT batch of the tag-sweep backlog
(claim_resource_batch, an atomic claim tool) and report on THEIR batch. The
SDK combines the copies (`instances_handoff: last`): counts SUM, lists concat.

This module owns the two things Python must — and it runs on the COMBINED
result AFTER the merge (the @chain reshape receives the combined payload +
the `_instances` pool), because a ratio computed per-copy would be wrong when
the copies' numbers sum:

@chain  enforce_coverage_floor — recompute coverage_pct from the SUMMED
        total_resources / fully_compliant, and force requires_sre_action when
        coverage falls below store.COVERAGE_FLOOR_PCT. (Never trust a per-copy
        ratio: 0.9 + 0.9 must not become 1.8 — recompute from the counts.)

@chain  enforce_budget_floor — THE DETERMINISTIC BUDGET-OVERAGE THRESHOLD. A
        forecast month-end overage at/above store.BUDGET_OVERAGE_FLOOR_PCT is
        a human-action overage, not a soft alert — requires_analyst_action is
        FORCED true. Reproducible arithmetic the LLM cannot soften.

DEV (no connector): the claim/read tools fall back to the seeded store so the
sweep produces a real governance report day-0.
"""
from __future__ import annotations

import json
from typing import Any

from leafmesh import LeafMeshLogger, chain

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
    return default


# ── The coverage floor as a PURE helper (unit-testable) ───────────────
def apply_coverage_floor(result: dict[str, Any]) -> dict[str, Any]:
    """Recompute coverage_pct from the (merged) counts and force
    requires_sre_action below the floor. Pure — no per-copy ratio trusted."""
    result = dict(result) if isinstance(result, dict) else {}
    total = int(result.get("total_resources") or 0)
    compliant = int(result.get("fully_compliant") or 0)
    coverage = round(compliant / total, 4) if total > 0 else 0.0
    result["coverage_pct"] = coverage
    below = total > 0 and coverage < store.COVERAGE_FLOOR_PCT
    result["requires_sre_action"] = bool(result.get("requires_sre_action")) or below
    if below and not result.get("compliance_briefing"):
        result["compliance_briefing"] = (
            f"TAG GAP: {compliant}/{total} resources compliant "
            f"({coverage:.0%} < {store.COVERAGE_FLOOR_PCT:.0%}) — SRE action required.")
    if below:
        logger.warning(f"[finops] COVERAGE FLOOR forced SRE action — "
                       f"{coverage:.0%} of {total} resources compliant")
    return result


# ── The budget floor as a PURE helper (unit-testable) ─────────────────
def apply_budget_floor(result: dict[str, Any],
                       cost_center: str,
                       forecast_cents: int) -> dict[str, Any]:
    """Force requires_analyst_action when the forecast overage clears the
    budget floor. Pure — the caller supplies the cost-center + forecast."""
    result = dict(result) if isinstance(result, dict) else {}
    over = store.budget_overage(cost_center, forecast_cents)
    result["cost_center"] = cost_center
    result["monthly_budget_cents"] = over["monthly_budget_cents"]
    result["forecast_month_end_cents"] = int(forecast_cents or 0)
    result["forecast_overage_pct"] = over["forecast_overage_pct"]
    forced = bool(over["requires_action"])
    result["requires_analyst_action"] = bool(result.get("requires_analyst_action")) or forced
    if forced and not result.get("budget_briefing"):
        result["budget_briefing"] = (
            f"OVERAGE: {cost_center} forecast {forecast_cents}c vs budget "
            f"{over['monthly_budget_cents']}c "
            f"(+{over['forecast_overage_pct']:.0%}) — analyst action required.")
    if forced:
        logger.warning(f"[finops] BUDGET FLOOR forced analyst action — {cost_center} "
                       f"overage +{over['forecast_overage_pct']:.0%}")
    return result


async def enforce_coverage_floor(result: Any, ctx: Any) -> Any:
    """@chain wrapper — recompute the ratio on the COMBINED result post-merge."""
    return apply_coverage_floor(_parse(result))


async def enforce_budget_floor(result: Any, ctx: Any) -> Any:
    """@chain wrapper — apply the budget-overage threshold. Carries the
    cost-center + forecast via private keys set by the body."""
    result = _parse(result)
    cost_center = str(result.pop("_budget_cost_center", "") or result.get("cost_center") or "")
    forecast = int(result.pop("_budget_forecast_cents", 0) or result.get("forecast_month_end_cents") or 0)
    if not cost_center:
        return result
    return apply_budget_floor(result, cost_center, forecast)


@chain(enforce_coverage_floor, enforce_budget_floor)
async def finops_governance_agent(llm_response: Any, input_data: Any, context: Any) -> Any:
    """Map one copy's batch report into the governance yields; the chain
    recomputes the ratio + enforces the budget floor on the merged result."""
    raw = _parse(llm_response)
    input_data = input_data if isinstance(input_data, dict) else {}
    mode = str(_pick(input_data, "mode", "") or raw.get("mode") or "tag_sweep")
    cost_center = str(_pick(input_data, "cost_center", "") or raw.get("cost_center") or "")

    # Forecast: the model's number, else the input, else a dev estimate (MTD×2).
    forecast = int(raw.get("forecast_month_end_cents") or 0)
    if not forecast:
        mtd = int(_pick(input_data, "mtd_cents", 0) or 0)
        forecast = int(_pick(input_data, "forecast_month_end_cents", 0) or 0) or (mtd * 2)

    result: dict[str, Any] = {
        "mode": mode,
        "total_resources": int(raw.get("total_resources") or 0),
        "fully_compliant": int(raw.get("fully_compliant") or 0),
        "coverage_pct": 0.0,                       # recomputed post-merge by the floor
        "missing_tag_breakdown": raw.get("missing_tag_breakdown") or {},
        "untagged_cost_cents_per_month": int(raw.get("untagged_cost_cents_per_month") or 0),
        "requires_sre_action": bool(raw.get("requires_sre_action") or False),
        "compliance_briefing": str(raw.get("compliance_briefing") or ""),
        "cost_center": cost_center,
        "mtd_cents": int(_pick(input_data, "mtd_cents", 0) or raw.get("mtd_cents") or 0),
        "monthly_budget_cents": int(raw.get("monthly_budget_cents") or 0),
        "forecast_month_end_cents": forecast,
        "forecast_overage_pct": float(raw.get("forecast_overage_pct") or 0),
        "requires_analyst_action": bool(raw.get("requires_analyst_action") or False),
        "budget_briefing": str(raw.get("budget_briefing") or ""),
        # Private carry → consumed by enforce_budget_floor (only bites in budget mode).
        "_budget_cost_center": cost_center if mode == "budget" else "",
        "_budget_forecast_cents": forecast if mode == "budget" else 0,
    }
    return result
