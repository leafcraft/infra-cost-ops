"""Day-0 tool surface for the infra-cost-ops pod.

These are the tools the LLM agents call, in the turn, to PULL cloud
telemetry / CMDB / commitment inventory / drift / tags / budgets. Each reads
the seed dev store (agency/_shared/store.py) so the pod runs with NO
connector wired, and **each announces itself**: when there is no real source
and no seed, it returns `available: false` and says why — never a synthetic
success that looks like a real read (doctrine A4/A5).

`claim_resource_batch` is the ATOMIC claim tool for Gwen's fanned-out tag
sweep (`scaling.copies.count: N`): each parallel copy claims a DISJOINT batch of the
backlog under a lock, so the copies never double-count the same resource.

To go live, wire the Cost Explorer / CloudWatch / CMDB mcp block in
the configuration (each named action becomes its own tool); these dev tools are
the fallback contract the real connector must satisfy. Whether a real cloud
connector is wired is read from the vault (record 'cloudops_mcp'), never
from .env.
"""
from __future__ import annotations

import os

from leafmesh import global_tool, LeafMeshLogger

try:
    from agency._shared import store
    from agency._shared.wiring import vault_record_exists
except ImportError:  # pragma: no cover — direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agency._shared import store  # type: ignore
    from agency._shared.wiring import vault_record_exists  # type: ignore

logger = LeafMeshLogger(__name__)


async def _connector_wired() -> bool:
    """True if a real cloud connector is wired: the vault holds 'cloudops_mcp'."""
    return await vault_record_exists("cloudops_mcp")


@global_tool(category="costops")
async def read_telemetry(tune_id: str) -> dict:
    """Read the cost/usage telemetry for an anomaly: utilisation (avg/p95 CPU),
    current spec, monthly cost, resource kind. Dev: the seed store; prod: Cost
    Explorer + CloudWatch. Announces available:false when there is nothing to
    read.

    Args:
        tune_id: The anomaly / tune to read telemetry for (e.g. "tune-2026-0612-ec2-idle").
    """
    tel = store.telemetry(str(tune_id).strip())
    if not tel:
        if await _connector_wired():
            return {"available": False, "reason": "connector wired but returned nothing for this tune_id"}
        return {"available": False,
                "reason": f"no seed telemetry for {tune_id!r}; wire the cloudops_mcp vault "
                          f"connection and the mcp block on cost_anomaly_agent"}
    logger.info(f"read_telemetry tune={tune_id} kind={tel.get('resource_kind')}")
    return {"available": True, **tel}


@global_tool(category="costops")
def read_blast_radius(resource_id: str) -> dict:
    """Read a resource's downstream fan-out + whether it touches a critical
    resource (auth/billing/security/kms/iam), from the CMDB dependency graph.
    Dev: the seed store; prod: the CMDB.

    Args:
        resource_id: The resource whose blast-radius to read (e.g. "db-prod-orders-1").
    """
    rid = str(resource_id).strip()
    if not rid:
        return {"available": False, "reason": "no resource_id given"}
    blast = store.blast_radius(rid)
    return {"available": True, "resource_id": rid, **blast}


@global_tool(category="costops")
def read_commitment_inventory() -> dict:
    """Read the RI + Savings-Plan inventory (expected vs actual utilisation,
    on-demand burn, expiry). Dev: the seed store; prod: AWS RI/SP, GCP CUDs."""
    inv = store.commitment_inventory()
    ris = inv.get("reserved_instances") or []
    sps = inv.get("savings_plans") or []
    if not ris and not sps:
        return {"available": False, "reason": "no commitment inventory; wire the cloud commitments API"}
    return {"available": True, **inv}


@global_tool(category="costops")
def read_drift_state() -> dict:
    """Read the last terraform plan / Pulumi preview drift set. Dev: the seed
    store; prod: the IaC planner."""
    d = store.drift_state()
    items = d.get("items") or []
    if not items:
        return {"available": False, "reason": "no drift set; wire the IaC planner"}
    return {"available": True, **d}


@global_tool(category="costops")
def read_workload_governance(workload_id: str) -> dict:
    """Read a workload's data-classes + applicable frameworks + current region
    from the data-governance registry. Dev: the seed store; prod: the
    governance service.

    Args:
        workload_id: The workload to read binding mandates for (e.g. "orders-primary-db").
    """
    wid = str(workload_id).strip()
    gov = store.workload_governance(wid)
    if not gov:
        return {"available": False, "reason": f"no governance record for {wid!r}"}
    return {"available": True, **gov}


@global_tool(category="costops")
def read_tag_inventory(cost_center: str = "") -> dict:
    """Read the tag/CMDB inventory (resource_id, kind, cost_center, env, owner,
    monthly cost) used for a tag-compliance sweep. Dev: the seed store; prod:
    the Resource Groups Tagging API / CMDB.

    Args:
        cost_center: Optional cost-center filter; empty reads the whole inventory.
    """
    rows = store.tag_inventory(str(cost_center).strip())
    if not rows:
        return {"available": False, "reason": "no tag inventory; wire the CMDB / tagging API"}
    return {"available": True, "resources": rows}


@global_tool(category="costops")
def read_budget(cost_center: str) -> dict:
    """Read a cost-center's monthly budget. Dev: the seed store; prod: AWS/GCP/
    Azure Budgets.

    Args:
        cost_center: The cost-center to read the budget for (e.g. "CC-ORDERS").
    """
    cc = str(cost_center).strip()
    b = store.budget(cc)
    if not b:
        return {"available": False, "reason": f"no budget for cost-center {cc!r}"}
    return {"available": True, **b}


@global_tool(category="costops")
def claim_resource_batch(batch_size: int = 5) -> dict:
    """ATOMICALLY claim up to `batch_size` resources off the tag-sweep backlog
    for THIS copy to work — each fanned-out copy (copies.count: N) gets a DISJOINT
    batch, so the backlog is split, never double-counted. Dev: a lock-guarded
    pop from the seed backlog; prod: an atomic `LPOP count` / `UPDATE ... SKIP
    LOCKED RETURNING` against your queue.

    Returns available:false with an empty batch when the backlog is drained.

    Args:
        batch_size: How many resources this copy should claim (default 5).
    """
    try:
        n = max(1, int(batch_size))
    except (TypeError, ValueError):
        n = 5
    claimed = store.claim_tag_batch(n)
    if not claimed:
        return {"available": False, "reason": "tag-sweep backlog drained", "resources": []}
    logger.info(f"claim_resource_batch claimed {len(claimed)} resource(s)")
    return {"available": True, "resources": claimed, "claimed_count": len(claimed)}
