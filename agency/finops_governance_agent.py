"""finops_governance_agent — FinOps connector SHAPER + budget-overage floor.

finops_governance_agent is a programmatic `integration: mcp` agent: its
results come from YOUR cloud billing/cost API (AWS Cost Explorer / GCP
Cloud Billing / Azure Cost Management) over an MCP server. This module is
the name-matched INTELLIGENCE SHAPER — it maps the raw connector result
into the declared yields and enforces the one floor the LLM/connector
must not waive:

@chain        enforce_budget_floor — THE DETERMINISTIC BUDGET-OVERAGE
              THRESHOLD. A forecast month-end overage at/above
              store.BUDGET_OVERAGE_FLOOR_PCT is a human-action overage,
              not a soft alert — requires_analyst_action is FORCED true.
              Reproducible arithmetic over the budget the LLM/connector
              cannot soften.

DEV (no connector): when CLOUD_BILLING_MCP_URL is unset the connector
returns nothing, so this shaper falls back to the seeded store budgets so
the budget_check entry still produces a real governance report day-0.
PROD: the MCP connector result flows in as `connector_result` and the
shaper maps + floors it.
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
            f"(+{over['forecast_overage_pct']:.0%}) — analyst action required."
        )
    if forced:
        logger.warning(
            f"[finops] BUDGET FLOOR forced analyst action — {cost_center} "
            f"overage +{over['forecast_overage_pct']:.0%}"
        )
    return result


async def enforce_budget_floor(result: Any, ctx: Any) -> Any:
    """@chain wrapper — apply the budget-overage threshold floor over the
    shaped report. Carries the cost-center + forecast via private keys."""
    result = _parse(result)
    cost_center = str(result.pop("_budget_cost_center", "") or result.get("cost_center") or "")
    forecast = int(result.pop("_budget_forecast_cents", 0) or result.get("forecast_month_end_cents") or 0)
    if not cost_center:
        return result
    return apply_budget_floor(result, cost_center, forecast)


@chain(enforce_budget_floor)
async def finops_governance_agent(connector_result: Any, input_data: Any, context: Any) -> Any:
    """Map the cloud-billing MCP result into the governance yields; the
    chain enforces the budget-overage floor. DEV: the connector is unwired,
    so fall back to the seeded store budgets."""
    raw = _parse(connector_result)
    input_data = input_data if isinstance(input_data, dict) else {}
    mode = str(_pick(input_data, "mode", "") or raw.get("mode") or "budget")
    cost_center = str(_pick(input_data, "cost_center", "") or raw.get("cost_center") or "")

    # Forecast: prefer the connector's number; else the input; else a dev
    # estimate of MTD × 2 from the input so the floor has something real.
    forecast = int(raw.get("forecast_month_end_cents") or 0)
    if not forecast:
        mtd = int(_pick(input_data, "mtd_cents", 0) or 0)
        forecast = int(_pick(input_data, "forecast_month_end_cents", 0) or 0) or (mtd * 2)

    result: dict[str, Any] = {
        "mode": mode,
        "total_resources": int(raw.get("total_resources") or 0),
        "fully_compliant": int(raw.get("fully_compliant") or 0),
        "coverage_pct": float(raw.get("coverage_pct") or 0),
        "missing_tag_breakdown": raw.get("missing_tag_breakdown") or {},
        "untagged_cost_cents_per_month": int(raw.get("untagged_cost_cents_per_month") or 0),
        "requires_sre_action": bool(raw.get("requires_sre_action") or False),
        "compliance_briefing": str(raw.get("compliance_briefing") or ""),
        "cost_center": cost_center,
        "mtd_cents": int(_pick(input_data, "mtd_cents", 0) or raw.get("mtd_cents") or 0),
        "monthly_budget_cents": int(raw.get("monthly_budget_cents") or 0),
        "forecast_month_end_cents": forecast,
        "forecast_overage_pct": float(raw.get("forecast_overage_pct") or 0),
        "burn_trend_pct": float(raw.get("burn_trend_pct") or 0),
        "anomaly_days_count": int(raw.get("anomaly_days_count") or 0),
        "requires_analyst_action": bool(raw.get("requires_analyst_action") or False),
        "budget_briefing": str(raw.get("budget_briefing") or ""),
        # Private carry → consumed by enforce_budget_floor.
        "_budget_cost_center": cost_center,
        "_budget_forecast_cents": forecast,
    }
    return result
