"""Dev-mode source store for infra-cost-ops — the systems-of-record stand-in.

Stands in for the customer's cloud telemetry + commitment inventory + IaC
state + tag/CMDB systems so the WHOLE tune chain (telemetry → classify →
right-size → safety-gate → compliance-veto → tune → audit) runs end-to-end
BEFORE any connector is wired. Every read here is the contract a production
connector must satisfy; swap the body for the real call and the agents are
unchanged:

  * Cost / usage telemetry  → AWS Cost Explorer + CloudWatch / GCP
                              Monitoring / Azure Monitor (the anomaly the
                              webhook fired carries an id; the detail is
                              pulled here)
  * Commitment / RI inventory → AWS RI + Savings Plans / GCP CUDs /
                              Azure Reservations
  * IaC drift state         → the last `terraform plan` / Pulumi preview
  * Resource tags / CMDB    → Resource Groups Tagging API / CMDB — the
                              blast-radius + data-tier floors read these
  * Workload data-classes   → the data-governance registry (which
                              frameworks apply to which workload)
  * Budgets                 → AWS Budgets / GCP Budgets / Azure Cost Mgmt

Setting the connector env vars alone does nothing until you wire the calls
here. This is a SEEDED dev store, never the system of record (Rule 7): it
ships a realistic wasted-idle anomaly, a data-tier RDS anomaly, an RI/SP
commitment inventory, and an IaC drift item so the demo works out of the
box. Applied tunes append to an in-store ledger so the idempotency
pre-flight sees real history accumulate as tunes flow.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
# Dev store location. Override INFRA_COST_OPS_STORE_DIR to relocate (tests
# point it at a temp dir). Irrelevant in prod — reads hit the cloud APIs.
_DIR = Path(os.environ.get("INFRA_COST_OPS_STORE_DIR", "./data"))

# ── Floors' tunables (deterministic, no LLM) ─────────────────────────
# Resource kinds that ARE the production data tier — never auto-tuned.
DATA_TIER_KINDS = frozenset({
    "rds", "aurora", "dynamodb", "elasticache", "redshift", "documentdb",
    "ebs", "s3", "efs", "fsx", "cloudsql", "spanner", "bigtable",
    "cosmosdb", "azuresql", "managed_disk", "database", "storage",
})
# Critical-resource keywords — drift / blast-radius escalation triggers.
CRITICAL_KEYWORDS = frozenset({
    "auth", "billing", "security", "kms", "iam", "secrets", "vault",
})
# A tune touching this many downstream resources (or a critical one) is
# never auto-applied regardless of what the LLM scored.
BLAST_RADIUS_AUTO_CAP = 5
# Budget-overage floor: a forecast overage at/above this fraction is an
# overage that requires a human, not an auto-noted alert.
BUDGET_OVERAGE_FLOOR_PCT = 0.10
# Tag-coverage floor: a sweep whose compliant fraction falls below this needs
# SRE action, not an auto-noted alert.
COVERAGE_FLOOR_PCT = 0.90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(k: str) -> Path:
    _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR / f"{k}.json"


def _load(k: str) -> Any:
    p = _path(k)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def _save(k: str, d: Any) -> None:
    p = _path(k)
    t = p.with_suffix(".json.tmp")
    t.write_text(json.dumps(d, indent=2, default=str))
    t.replace(p)


def _seeded(key: str, seed: Any) -> Any:
    s = _load(key)
    if s is None:
        s = seed
        _save(key, s)
    return s


# ════════════════════════════════════════════════════════════════════
# SEED DATA — realistic telemetry / commitments / drift / tags
# ════════════════════════════════════════════════════════════════════

# Cost / usage telemetry, keyed by tune_id. The anomaly webhook fires an
# id; the detail (utilisation, current spec, cost) is pulled from here.
# Two anchor anomalies drive the README story end-to-end:
#   · the SAFE wasted_idle resize (auto-applies through every floor)
#   · the DATA-TIER rds anomaly (the safety floor forces SRE regardless)
_SEED_TELEMETRY: dict[str, Any] = {
    "tune-2026-0612-ec2-idle": {
        "tune_id": "tune-2026-0612-ec2-idle",
        "cloud": "aws", "resource_id": "i-0a1b2c3d", "resource_kind": "ec2",
        "anomaly_type": "wasted_idle",
        "observed_avg_cpu_pct_7d": 3.4, "observed_p95_cpu_pct_7d": 7.1,
        "current_spec": {"instance_type": "m5.4xlarge", "vcpu": 16, "mem_gib": 64},
        "monthly_cost_cents": 184220, "production_tier": False,
        "workload_id": "batch-etl-nightly",
    },
    "tune-2026-0613-rds-overprov": {
        "tune_id": "tune-2026-0613-rds-overprov",
        "cloud": "aws", "resource_id": "db-prod-orders-1", "resource_kind": "rds",
        "anomaly_type": "drift",
        "observed_avg_cpu_pct_7d": 11.0, "observed_p95_cpu_pct_7d": 22.0,
        "current_spec": {"instance_class": "db.r5.4xlarge", "vcpu": 16, "mem_gib": 128},
        "monthly_cost_cents": 612000, "production_tier": True,
        "workload_id": "orders-primary-db",
    },
}

# Right-sizing reference — for each instance family, the next-smaller step
# and its cost ratio. The savings math reads this (PROD: the pricing API).
_SEED_RIGHTSIZE_LADDER: dict[str, Any] = {
    "m5.4xlarge": {"step_down": "m5.large", "cost_ratio": 0.125},
    "m5.2xlarge": {"step_down": "m5.large", "cost_ratio": 0.25},
    "m5.xlarge":  {"step_down": "m5.large", "cost_ratio": 0.5},
    "m5.large":   {"step_down": "m5.large", "cost_ratio": 1.0},
    "db.r5.4xlarge": {"step_down": "db.r5.xlarge", "cost_ratio": 0.25},
}

# Commitment / RI + Savings-Plan inventory. PROD: AWS RI/SP, GCP CUDs.
_SEED_COMMITMENTS: dict[str, Any] = {
    "as_of_utc": None,
    "reserved_instances": [
        {"commit_id": "ri-r5-2y-2024-07", "type": "reserved_instance",
         "family": "r5", "region": "us-east-1", "count": 8,
         "expected_use_pct": 0.95, "actual_use_pct": 0.92,
         "on_demand_monthly_cents": 472400, "expires_at_utc": "2026-07-15T00:00:00Z"},
        {"commit_id": "ri-m5-1y-2025-02", "type": "reserved_instance",
         "family": "m5", "region": "us-east-1", "count": 12,
         "expected_use_pct": 0.90, "actual_use_pct": 0.41,    # under-used → waste
         "on_demand_monthly_cents": 288000, "expires_at_utc": "2026-02-01T00:00:00Z"},
    ],
    "savings_plans": [
        {"commit_id": "sp-1y-compute-2025-08", "type": "savings_plan",
         "commitment_per_hour_usd": 4.2, "term_remaining_months": 14,
         "expected_use_pct": 0.95, "actual_use_pct": 0.73},   # drifted < 80%
    ],
}

# IaC drift — the last terraform plan / Pulumi preview. PROD: the planner.
_SEED_DRIFT: dict[str, Any] = {
    "detected_at_utc": None,
    "items": [
        {"resource": "aws_security_group.web_ingress",
         "attribute": "ingress[3].cidr_blocks",
         "intended": "10.0.0.0/8", "actual": "0.0.0.0/0"},          # critical: security
        {"resource": "aws_iam_role.batch_exec",
         "attribute": "assume_role_policy",
         "intended": "scoped", "actual": "wildcard"},               # critical: iam
        {"resource": "aws_s3_bucket.assets",
         "attribute": "tags.cost_center",
         "intended": "CC-WEB", "actual": ""},                       # non-critical
    ],
}

# Resource tags / CMDB — the blast-radius + data-tier floors read these.
# `downstream` is the dependency fan-out (what a change ripples into).
_SEED_RESOURCE_TAGS: dict[str, Any] = {
    "i-0a1b2c3d": {"resource_id": "i-0a1b2c3d", "kind": "ec2",
                   "cost_center": "CC-DATA", "env": "prod",
                   "data_classes": [], "downstream": ["sqs-etl-queue"]},
    "db-prod-orders-1": {"resource_id": "db-prod-orders-1", "kind": "rds",
                         "cost_center": "CC-ORDERS", "env": "prod",
                         "data_classes": ["pii", "financial"],
                         "downstream": ["api-orders", "api-checkout",
                                        "worker-fulfilment", "report-finance",
                                        "cache-orders", "search-orders"]},
}

# Workload data-classification + applicable frameworks. The compliance
# veto floor reads this — which residency/encryption mandates bind a
# workload. PROD: the data-governance registry.
_SEED_WORKLOAD_GOVERNANCE: dict[str, Any] = {
    "orders-primary-db": {
        "workload_id": "orders-primary-db",
        "data_classes": ["pii", "financial"],
        "applicable_frameworks": ["EU_GDPR", "CHINA_PIPL"],
        "current_region": "eu-west-1",
    },
    "batch-etl-nightly": {
        "workload_id": "batch-etl-nightly",
        "data_classes": [],
        "applicable_frameworks": [],
        "current_region": "us-east-1",
    },
}

# Data-residency allow-lists per framework (region prefixes). PROD: a
# compliance policy service.
RESIDENCY_ALLOW: dict[str, Any] = {
    "EU_GDPR": {"applies_to": {"pii", "customer_data"},
                "regions": {"eu-west-1", "eu-central-1", "eu-north-1",
                            "eu-south-1", "europe-west", "europe-central"}},
    "INDIA_DPDP": {"applies_to": {"pii"}, "regions": {"ap-south-1", "asia-south1"}},
    "CHINA_PIPL": {"applies_to": {"pii", "financial"},
                   "regions": {"cn-north-1", "cn-northwest-1"}},
    "RUSSIA_FZ242": {"applies_to": {"pii"}, "regions": {"ru-central1"}},
    "CANADA_PIPEDA": {"applies_to": {"pii"},
                      "regions": {"ca-central-1", "northamerica-northeast"}},
    "AUSTRALIA_PA": {"applies_to": {"pii"},
                     "regions": {"ap-southeast-2", "australia-southeast1"}},
}

# Budgets per cost-center. The budget-overage floor reads this.
_SEED_BUDGETS: dict[str, Any] = {
    "CC-DATA":   {"cost_center": "CC-DATA",   "monthly_budget_cents": 5000000},
    "CC-ORDERS": {"cost_center": "CC-ORDERS", "monthly_budget_cents": 9000000},
    "CC-WEB":    {"cost_center": "CC-WEB",    "monthly_budget_cents": 3000000},
}

# Tag-compliance backlog — the resources Gwen's fanned-out sweep works
# through. Every required tag present = compliant; a blank required tag
# (cost_center / env / owner) = non-compliant. PROD: the Resource Groups
# Tagging API / CMDB inventory. Sized so a 2-copy fan-out splits it.
REQUIRED_TAGS = ("cost_center", "env", "owner")
_SEED_TAG_BACKLOG: list[dict[str, Any]] = [
    {"resource_id": "i-0a1b2c3d", "kind": "ec2", "monthly_cost_cents": 184220,
     "tags": {"cost_center": "CC-DATA", "env": "prod", "owner": "data-eng"}},
    {"resource_id": "db-prod-orders-1", "kind": "rds", "monthly_cost_cents": 612000,
     "tags": {"cost_center": "CC-ORDERS", "env": "prod", "owner": "orders"}},
    {"resource_id": "i-0e4f5g6h", "kind": "ec2", "monthly_cost_cents": 92110,
     "tags": {"cost_center": "CC-WEB", "env": "prod", "owner": ""}},          # missing owner
    {"resource_id": "s3-assets-cdn", "kind": "s3", "monthly_cost_cents": 41000,
     "tags": {"cost_center": "", "env": "prod", "owner": "web"}},            # missing cost_center
    {"resource_id": "gke-batch-pool", "kind": "gke", "monthly_cost_cents": 158900,
     "tags": {"cost_center": "CC-DATA", "env": "", "owner": ""}},            # missing env + owner
    {"resource_id": "lambda-thumbnailer", "kind": "lambda", "monthly_cost_cents": 3400,
     "tags": {"cost_center": "CC-WEB", "env": "prod", "owner": "web"}},
]


# ── Reads (the cloud systems of record) ──────────────────────────────
def telemetry(tune_id: str = "") -> dict[str, Any]:
    """Cost/usage detail for an anomaly. PROD: Cost Explorer + CloudWatch.
    Returns one anomaly or the whole book."""
    book = _seeded("telemetry", _SEED_TELEMETRY)
    return book.get(tune_id, {}) if tune_id else dict(book)


def resource_tags(resource_id: str = "") -> dict[str, Any]:
    """Tags / CMDB record incl. data_classes + downstream fan-out.
    PROD: Resource Groups Tagging API / CMDB."""
    book = _seeded("resource_tags", _SEED_RESOURCE_TAGS)
    return book.get(resource_id, {}) if resource_id else dict(book)


def commitment_inventory() -> dict[str, Any]:
    """RI + Savings-Plan inventory. PROD: AWS RI/SP, GCP CUDs."""
    inv = _seeded("commitments", _SEED_COMMITMENTS)
    if isinstance(inv, dict) and not inv.get("as_of_utc"):
        inv = {**inv, "as_of_utc": _now()}
    return inv


def drift_state() -> dict[str, Any]:
    """The last terraform plan / Pulumi preview drift set."""
    d = _seeded("drift", _SEED_DRIFT)
    if isinstance(d, dict) and not d.get("detected_at_utc"):
        d = {**d, "detected_at_utc": _now()}
    return d


def workload_governance(workload_id: str = "") -> dict[str, Any]:
    """Data-class + applicable-framework registry for a workload."""
    book = _seeded("workload_governance", _SEED_WORKLOAD_GOVERNANCE)
    return book.get(workload_id, {}) if workload_id else dict(book)


def budget(cost_center: str) -> dict[str, Any]:
    """Monthly budget for a cost-center. PROD: AWS/GCP/Azure Budgets."""
    return _seeded("budgets", _SEED_BUDGETS).get(cost_center, {})


def tag_inventory(cost_center: str = "") -> list[dict[str, Any]]:
    """The full tag/CMDB inventory (read-only, does not consume the sweep
    backlog). PROD: the Resource Groups Tagging API / CMDB."""
    rows = list(_SEED_TAG_BACKLOG)
    if cost_center:
        rows = [r for r in rows if (r.get("tags") or {}).get("cost_center") == cost_center]
    return rows


def claim_tag_batch(batch_size: int) -> list[dict[str, Any]]:
    """ATOMICALLY pop up to `batch_size` resources off the tag-sweep backlog
    queue. Each fanned-out copy (instances: N) claims a DISJOINT batch, so the
    copies never double-count. Lock-guarded + persisted so concurrent copies in
    one process split the queue. The queue REFILLS from the seed backlog once
    fully drained, so repeated sweeps keep working day-0. PROD: an atomic
    `LPOP count` / `UPDATE ... SKIP LOCKED RETURNING` on your real queue."""
    n = max(1, int(batch_size or 1))
    with _LOCK:
        q = _load("tag_backlog_queue")
        if not isinstance(q, list) or not q:
            q = [dict(r) for r in _SEED_TAG_BACKLOG]   # (re)fill on first call / drain
        claimed = q[:n]
        remaining = q[n:]
        _save("tag_backlog_queue", remaining)
    return claimed


# ════════════════════════════════════════════════════════════════════
# DOMAIN MATH the floors rely on (deterministic, no LLM)
# ════════════════════════════════════════════════════════════════════
def rightsizing_savings(current_spec: dict[str, Any],
                        monthly_cost_cents: int) -> dict[str, Any]:
    """Propose the next-smaller spec + the savings it yields, off the
    pricing ladder. PROD: the cloud pricing API. Returns {proposed_spec,
    daily_savings_cents, monthly_savings_cents} — {} when no step exists.
    """
    spec = current_spec if isinstance(current_spec, dict) else {}
    cur = str(spec.get("instance_type") or spec.get("instance_class") or "")
    ladder = _seeded("rightsize_ladder", _SEED_RIGHTSIZE_LADDER).get(cur)
    if not ladder or ladder.get("step_down") == cur:
        return {}
    cost = int(monthly_cost_cents or 0)
    proposed_monthly = int(round(cost * float(ladder["cost_ratio"])))
    monthly_savings = max(0, cost - proposed_monthly)
    key = "instance_class" if "instance_class" in spec else "instance_type"
    return {
        "proposed_spec": {**spec, key: ladder["step_down"]},
        "monthly_savings_cents": monthly_savings,
        "daily_savings_cents": int(round(monthly_savings / 30.0)),
    }


def blast_radius(resource_id: str) -> dict[str, Any]:
    """Count the downstream resources a change ripples into + whether any
    is a critical resource. The blast-radius floor reads this. PROD: CMDB
    dependency graph."""
    rec = resource_tags(resource_id) or {}
    downstream = [str(d) for d in (rec.get("downstream") or [])]
    blob = " ".join([resource_id or "", *downstream]).lower()
    touches_critical = any(k in blob for k in CRITICAL_KEYWORDS)
    return {
        "downstream_count": len(downstream),
        "downstream": downstream,
        "touches_critical": touches_critical,
        "data_classes": list(rec.get("data_classes") or []),
    }


def is_data_tier(resource_kind: str, resource_id: str = "") -> bool:
    """Deterministic data-tier verdict from the resource kind (+ CMDB
    kind fallback). The safety floor uses this — the LLM cannot mark a
    database as non-data-tier."""
    kind = str(resource_kind or "").strip().lower()
    if kind in DATA_TIER_KINDS:
        return True
    cmdb_kind = str((resource_tags(resource_id) or {}).get("kind") or "").lower()
    return cmdb_kind in DATA_TIER_KINDS


def residency_violation(workload_id: str, to_region: str,
                        data_classes: list[str] | None = None,
                        frameworks: list[str] | None = None) -> dict[str, Any]:
    """Deterministic residency check for a region_change. A framework is
    VIOLATED when its applies_to overlaps the workload's data-classes AND
    the destination region is NOT in its allow-list. The compliance veto
    floor uses this — the LLM cannot clear a residency-violating move.

    Returns {vetoed, rule_violated, frameworks_checked}.
    """
    gov = workload_governance(workload_id) or {}
    classes = set(data_classes or gov.get("data_classes") or [])
    fws = list(frameworks or gov.get("applicable_frameworks") or [])
    region = str(to_region or "").split(":")[-1].strip().lower()
    for fw in fws:
        spec = RESIDENCY_ALLOW.get(fw)
        if not spec:
            continue
        if spec["applies_to"] & classes:
            allowed = {r.lower() for r in spec["regions"]}
            if not any(region == a or region.startswith(a) for a in allowed):
                return {"vetoed": True, "rule_violated": f"{fw}_residency",
                        "frameworks_checked": fws}
    return {"vetoed": False, "rule_violated": "", "frameworks_checked": fws}


def encryption_floor(frameworks: list[str] | None, to_value: str) -> dict[str, Any]:
    """Deterministic encryption-minimum check for an encryption_change.
    pci_dss/hipaa/fedramp_mod require AES-256 class; fedramp_hi requires
    FIPS-140. A to_value below the floor is a violation."""
    fws = {str(f).lower() for f in (frameworks or [])}
    spec = str(to_value or "").upper()
    requires_fips = "fedramp_hi" in fws
    requires_aes256 = bool(fws & {"pci_dss", "hipaa", "fedramp_mod"}) or requires_fips
    if requires_fips and "FIPS" not in spec:
        return {"vetoed": True, "rule_violated": "AES-256-GCM-FIPS140_required"}
    if requires_aes256 and "AES-256" not in spec:
        return {"vetoed": True, "rule_violated": "AES-256-GCM_required"}
    return {"vetoed": False, "rule_violated": ""}


def budget_overage(cost_center: str, forecast_month_end_cents: int) -> dict[str, Any]:
    """Forecast overage vs the cost-center budget. The budget-overage
    floor uses this — an overage at/above BUDGET_OVERAGE_FLOOR_PCT is a
    human-action overage, not an auto-noted alert."""
    b = budget(cost_center) or {}
    cap = int(b.get("monthly_budget_cents") or 0)
    forecast = int(forecast_month_end_cents or 0)
    if cap <= 0:
        return {"monthly_budget_cents": 0, "forecast_overage_pct": 0.0,
                "requires_action": False}
    over_pct = max(0.0, (forecast - cap) / cap)
    return {"monthly_budget_cents": cap,
            "forecast_overage_pct": round(over_pct, 4),
            "requires_action": over_pct >= BUDGET_OVERAGE_FLOOR_PCT}


# ── Applied-tune ledger (the idempotency pre-flight source) ──────────
def already_applied(tune_id: str) -> dict[str, Any]:
    """Return the prior applied record for a tune_id, or {}. The tuner's
    idempotency pre-flight reads this so a re-fired approval does not
    double-apply. PROD: query the change-management system."""
    if not tune_id:
        return {}
    rows = (_seeded("applied_ledger", {"rows": []}) or {}).get("rows", [])
    for r in rows:
        if r.get("tune_id") == tune_id:
            return r
    return {}


def record_applied(row: dict[str, Any]) -> None:
    """Append an applied tune so the idempotency pre-flight sees history
    accumulate. Called by cost_tuner after a successful apply. PROD: no-op
    (the change-management system already holds it)."""
    if not row.get("tune_id"):
        return
    with _LOCK:
        s = _seeded("applied_ledger", {"rows": []})
        rows = s.setdefault("rows", [])
        rows.append({
            "tune_id": str(row.get("tune_id") or ""),
            "rollback_token": str(row.get("rollback_token") or ""),
            "applied_at_utc": str(row.get("applied_at_utc") or _now()),
        })
        _save("applied_ledger", s)
