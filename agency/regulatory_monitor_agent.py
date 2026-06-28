"""regulatory_monitor_agent — SELF-RELIANT compliance officer + VETO floor.

The LLM (configs/config.yaml) writes the compliance narrative + cites the
mandate; this module owns the two things the LLM must NOT be trusted with:

@pre_compose  load_governance — pull the workload's data-classification +
              applicable frameworks + current region from the data-
              governance registry (the source store; the governance
              service in prod) so the veto reasons over the REAL binding
              mandates, not a hallucinated "I checked the policy".
              Self-reliant context pull.

@chain        enforce_compliance_veto — THE DETERMINISTIC RESIDENCY /
              ENCRYPTION VETO. A compliance breach is irreversible
              regulatory exposure, so the veto is reproducible arithmetic
              the LLM may NOT waive (first principle 6). It re-runs:
                · the residency check — for a region_change, a framework
                  is VIOLATED when its applies_to overlaps the workload's
                  data-classes AND the destination region is outside its
                  allow-list;
                · the encryption-minimum check — for an encryption_change,
                  a to_value below the strictest applicable floor.
              A deterministic violation FORCES vetoed=true + a real
              rule_violated + requires_sre_review, regardless of what the
              LLM scored. The floor only ever RAISES the veto — the LLM
              can never clear a violating change (fail-closed: when in
              doubt, veto).

Everything else (the narrative, the cross-session veto-ledger memory)
stays prompt-driven and SDK-managed.
"""
from __future__ import annotations

import json
import time
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
    uy = data.get("upstream_yields")
    if isinstance(uy, dict):
        if uy.get(key) not in (None, ""):
            return uy[key]
        for v in uy.values():
            if isinstance(v, dict) and v.get(key) not in (None, ""):
                return v[key]
    return default


# ── @pre_compose — pull the binding mandates for this workload ────────
def load_governance(data: Any, ctx: Any) -> dict[str, Any]:
    """Fetch the workload's data-classes + applicable frameworks + current
    region. PROD: the data-governance registry."""
    input_data = data if isinstance(data, dict) else {}
    workload_id = str(_pick(input_data, "workload_id", "") or "")
    gov = store.workload_governance(workload_id)
    return {
        "workload_id": workload_id,
        "governance": gov,
        "registry_data_classes": list(gov.get("data_classes") or []),
        "registry_frameworks": list(gov.get("applicable_frameworks") or []),
    }


def _ctx(context: Any) -> dict[str, Any]:
    prepared = (context or {}).get("prepared_data", {}) if isinstance(context, dict) else {}
    c = prepared.get("context", {}) if isinstance(prepared, dict) else {}
    return c if isinstance(c, dict) else {}


# ── The veto floor as a PURE helper (unit-testable) ───────────────────
def apply_veto_floor(result: dict[str, Any],
                     workload_id: str,
                     data_classes: list[str],
                     frameworks: list[str]) -> dict[str, Any]:
    """Re-run the residency + encryption checks deterministically and FORCE
    the veto on any violation. The LLM can never CLEAR a violating change.
    Pure — the caller supplies the workload mandates (read from store)."""
    result = dict(result) if isinstance(result, dict) else {}
    change = result.get("proposed_change") or {}
    if not isinstance(change, dict):
        change = {}

    kind = str(change.get("kind") or "").strip().lower()
    to_value = str(change.get("to_value") or "")
    tune_id = str(result.get("tune_id") or "")

    deterministic_veto = False
    rule = ""
    if kind == "region_change":
        check = store.residency_violation(workload_id, to_value, data_classes, frameworks)
        deterministic_veto = bool(check["vetoed"])
        rule = check["rule_violated"]
    elif kind == "encryption_change":
        check = store.encryption_floor(frameworks, to_value)
        deterministic_veto = bool(check["vetoed"])
        rule = check["rule_violated"]

    result["action"] = "check" if kind else "skipped"

    # Floor: a deterministic violation forces the veto regardless of the
    # LLM. The LLM may RAISE a veto but never lower a deterministic one.
    llm_veto = bool(result.get("vetoed"))
    vetoed = llm_veto or deterministic_veto
    result["vetoed"] = vetoed

    if vetoed:
        if deterministic_veto and not llm_veto:
            logger.warning(
                f"[regulatory] VETO FLOOR forced vetoed=true for tune={tune_id or '?'} "
                f"({rule}) — the LLM cleared a residency/encryption-violating change."
            )
        result["rule_violated"] = result.get("rule_violated") or rule or "compliance_violation"
        if not result.get("veto_id"):
            result["veto_id"] = f"veto-{tune_id or 'na'}-{int(time.time())}"
        result["requires_sre_review"] = True
    else:
        result["rule_violated"] = ""
        result["veto_id"] = ""
        result["requires_sre_review"] = False

    # Echo the canonical mandates (sorted), overriding any LLM drift.
    result["data_classes"] = sorted(set(data_classes))
    result["applicable_frameworks"] = sorted(set(frameworks))
    return result


async def enforce_compliance_veto(result: Any, ctx: Any) -> Any:
    """@chain wrapper — re-run the residency/encryption check from the
    store. The body attaches the resolved mandates as private `_veto_*`
    carry keys so this fires identically through the runtime and direct."""
    result = _parse(result)
    workload_id = str(result.pop("_veto_workload_id", "") or "")
    data_classes = list(result.pop("_veto_data_classes", []) or [])
    frameworks = list(result.pop("_veto_frameworks", []) or [])
    if not data_classes and not frameworks and workload_id:
        gov = store.workload_governance(workload_id)
        data_classes = list(gov.get("data_classes") or [])
        frameworks = list(gov.get("applicable_frameworks") or [])
    return apply_veto_floor(result, workload_id, data_classes, frameworks)


@pre_compose(context_processor=load_governance)
@chain(enforce_compliance_veto)
async def regulatory_monitor_agent(llm_response: Any, input_data: Any, context: Any) -> Any:
    """Surface the LLM's compliance verdict; the chain enforces the
    deterministic veto floor. The body resolves the binding mandates (it
    has input_data) and hands them to the floor via private carry keys."""
    result = _parse(llm_response)
    input_data = input_data if isinstance(input_data, dict) else {}
    pctx = _ctx(context)

    workload_id = (pctx.get("workload_id")
                   or str(_pick(input_data, "workload_id", "") or "")
                   or str(result.get("workload_id") or ""))
    # Prefer the registry mandates; fall back to the input lists.
    data_classes = (pctx.get("registry_data_classes")
                    or _pick(input_data, "workload_data_classes", [])
                    or result.get("data_classes") or [])
    frameworks = (pctx.get("registry_frameworks")
                  or _pick(input_data, "applicable_frameworks", [])
                  or result.get("applicable_frameworks") or [])
    if (not data_classes or not frameworks) and workload_id:
        gov = store.workload_governance(workload_id)
        data_classes = data_classes or list(gov.get("data_classes") or [])
        frameworks = frameworks or list(gov.get("applicable_frameworks") or [])

    # Identity passthrough + the proposed_change the floor checks.
    result["workload_id"] = workload_id
    result.setdefault("tune_id", str(_pick(input_data, "tune_id", "") or ""))
    if not result.get("proposed_change"):
        result["proposed_change"] = _pick(input_data, "proposed_change", {}) or {}

    # Private carry → consumed + stripped by enforce_compliance_veto.
    result["_veto_workload_id"] = workload_id
    result["_veto_data_classes"] = list(data_classes)
    result["_veto_frameworks"] = list(frameworks)
    return result
