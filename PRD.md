# infra-cost-ops — Infra Cost Ops — PRD

## Story

**Maya, an SRE at a mid-size SaaS company**, never opens this system — it
runs without her until a decision needs her judgment. At 02:14 the cloud
cost-explorer fires an anomaly webhook: an `m5.4xlarge` at 4% CPU for nine
days. `cost_anomaly_agent` classifies it and proposes the tune in one LLM
call — `wasted_idle`, 41,000¢/day, production-tier `false`,
`m5.4xlarge → m5.large`, `is_safe_tune: true`, `data_tier: false`. Safe and
non-data-tier, so the chain routes to `cost_tuner_agent`, which applies the
resize, returns a `rollback_token`, and writes to the WORM audit. The next
day an `rds` anomaly lands; it is a database, so the **safety-gate `@chain`
floor** re-derives `data_tier: true` from the resource kind and forces
`is_safe_tune: false` (regardless of what the LLM scored) — the chain routes
to **Maya** (`sre_human`).
The tune payload was stashed pre-gate, so when she approves, the tuner
recovers the spec and applies it. A no-show within 4 h escalates to the
**CTO**; a CTO no-show holds for review. Nothing irreversible happens
without a human, and nothing is lost across the hand-off.

## Flow

```
 cost-explorer webhook ─┐
 scheduled rightsize ───┤
 manual_action ─────────┴──▶ cost_anomaly_agent ──┬─ new_resource ─▶ commitments_agent
                                                  │                       │
                          safe & not data-tier ───┤                 savings>0 │  =0
                                                  ▼                       ▼     ▼
                                            cost_tuner_agent ◀── sre_human   audit
                                                  │              ▲  │  │
                            unsafe / data-tier ───┘ (approve)────┘  │  └─(reject)─▶ audit
                                                                    │(escalate)
 iac_commit ──▶ iac_drift_agent ── critical ─▶ sre_human          ▼
                       └─ clean ─▶ audit                       cto_human ─(approve)─▶ cost_tuner_agent
                                                                   └─(else)──▶ audit
 compliance_veto_check ─▶ regulatory_monitor_agent ─ vetoed ─▶ sre_human
                                  └─ clean ─▶ cost_tuner_agent

 weekly cron / RI/SP entries ─▶ commitments_agent  (Mon 07:00)
 tag/budget entries ──────────▶ finops_governance_agent (MCP connector; results stored)

 cost_tuner_agent ─▶ infra_audit_logger_agent   (hash-chained WORM terminus)
```

## Actor inventory

| Actor | Job title | Type |
|---|---|---|
| `cost_anomaly_agent` | Cloud cost analyst | `llm` |
| `commitments_agent` | Cloud commitments analyst | `llm` (cron) |
| `iac_drift_agent` | Infrastructure drift reconciler | `llm` |
| `regulatory_monitor_agent` | Compliance officer (cloud) | `llm` (cross-session memory) |
| `cost_tuner_agent` | Cloud change actuator | `programmatic` |
| `sre_human` | Site Reliability Engineer | `human` |
| `cto_human` | Chief Technology Officer | `human` |
| `finops_governance_agent` | FinOps governance analyst | `programmatic` (`mcp`) |
| `infra_audit_logger_agent` | Audit & compliance recorder | `external` (`custom`) |
| manager | Cost-ops supervisor | manager (learning router) |

## Data artifacts

- **CostEvent** (`data_structures`): `tune_id`, `cloud`, `resource_id`,
  `resource_kind`, `anomaly_type`, `received_at_utc` (required: `cloud`).
- **Tune payload** (stashed pre-gate by `cost_anomaly_agent`): `tune_id`,
  `current_spec`, `proposed_spec`, `daily_savings_cents`, `data_tier`,
  `is_safe_tune` — recovered by `cost_tuner_agent` after a HITL approval.
- **Audit entry** (WORM): `audit_event_id`, `written_at_utc`, `session_id`,
  `payload`, `hash_chain_prev`, `hash_chain_current` (SHA-256 chain).
- **Veto ledger**: per-tune verdicts held in `regulatory_monitor_agent`
  cross-session memory (`cross_session_limit: 100`).

## Per-agent spec

Each non-human, non-terminal agent is **self-reliant** — it pulls its own
context (`@pre_compose`) and enforces its own deterministic floor (`@chain`)
the LLM cannot waive. The dev store (`agency/_shared/store.py`) is the
day-0 stand-in for every connector.

- **cost_anomaly_agent** — `llm`, `temperature 0.0`, knowledge
  `cost_patterns`, `enforce_yields` (retry 2). Two-stage prompt (classify +
  propose). Module (`agency/cost_anomaly_agent.py`):
  `@pre_compose load_telemetry` pulls the telemetry + CMDB blast-radius;
  `@chain enforce_safety_floor` re-derives `data_tier` (from the resource
  KIND), `blast_radius` + `touches_critical` (from the CMDB), and forces
  `is_safe_tune=false` on any data-tier / new_resource / critical-resource /
  over-cap tune — the LLM can only be DOWNGRADED — and fills the right-sizing
  math when the LLM omitted it; `@chain stash_tune` stashes the floored
  payload by session before the SRE gate. Yields add `blast_radius`,
  `touches_critical`.
- **commitments_agent** — `llm`, cron `0 7 * * 1`, knowledge
  `cloud_commit_catalog`, `enforce_yields`. Module:
  `@pre_compose load_commitments` pulls the RI/SP inventory;
  `@chain enforce_savings_floor` recomputes wasted spend from the inventory
  and clamps the LLM down to that basis (a real reserved-capacity forecast
  overrides), **fail-closed** to zero with no basis → audit, not the SRE;
  `@compose` ships the SRE the full forecast and the audit logger the
  terminus summary.
- **iac_drift_agent** — `llm`, `enforce_yields`. Module:
  `@pre_compose load_drift_state` pulls the last plan's drift set;
  `@chain enforce_blast_radius` re-scans every item against the critical
  taxonomy (auth/billing/security/kms/iam/secrets/vpc/…) and forces
  `critical_drift=true` on any hit — the LLM cannot clear it. Yields add
  `critical_resources`.
- **regulatory_monitor_agent** — `llm`, `cross_session` memory,
  `enforce_yields`. Module: `@pre_compose load_governance` pulls the
  workload's binding data-classes + frameworks; `@chain
  enforce_compliance_veto` re-runs the residency/encryption check from the
  store and forces `vetoed=true` on any violation (fail-closed — the LLM
  may raise a veto, never clear one). Veto ledger in agent memory.
- **cost_tuner_agent** — `programmatic`, `execute`. Side-effecting actuator
  (`agency/cost_tuner_agent.py`); **fail-closed** when no payload
  recoverable (stash / upstream_yields / human_data) AND **idempotent** —
  a re-fired approval for a `tune_id` already in the applied ledger
  (`store.already_applied`) is a no-op returning the prior rollback token.
- **sre_human / cto_human** — `human`, `dual`, `fallback_on_timeout`
  default-deny (SRE 14400 s → CTO; CTO 28800 s → hold). `operator_ids`
  left unset → shared inbox.
- **finops_governance_agent** — `programmatic`, `integration: mcp`,
  `connector_config.url = ${CLOUD_BILLING_MCP_URL}`,
  `tool_name: get_cost_and_usage`, `auto_store_response/yields`. Module is
  the connector **shaper** (maps the raw MCP result into the yields, falls
  back to the seeded store budgets when unwired) + `@chain
  enforce_budget_floor` (a forecast overage ≥ `BUDGET_OVERAGE_FLOOR_PCT`
  forces `requires_analyst_action`). Cron commented out until wired.
- **infra_audit_logger_agent** — `external`, `framework: custom`,
  `local_file` sink. Registered via `mesh.intelligence()` in `main.py`
  (`agency/_shared/audit_logger.py`). `can_call: []` (terminus).

## Entry points

13 total: `cost_anomaly_webhook`, `scheduled_rightsize`, `manual_action`
(→ cost_anomaly); `scheduled_reserved`, `weekly_commit_review`,
`ri_expiry_alert`, `savings_plan_review` (→ commitments); `iac_commit`
(→ iac_drift); `tag_compliance_sweep`, `tag_inventory_ingest`,
`budget_check` (→ finops_governance); `direct_call_cost_tuner`
(→ cost_tuner, programmatic bypass); `compliance_veto_check`
(→ regulatory_monitor).

## Controls / trust

- **Default-deny HITL**: SRE and CTO gates fall back on timeout; SRE → CTO,
  CTO → hold-for-review. `fallback_response.message` matches a real route.
- **Fail-closed + idempotent actuator**: `cost_tuner_agent` refuses to apply
  when no tune payload is recoverable, and a re-fired approval for an
  already-applied `tune_id` is a no-op — an unknown change is never pushed
  live and an approval is never double-applied.
- **Deterministic `@chain` floors** the LLM cannot waive: the safety gate
  (data-tier / new_resource / critical-resource / over-cap → `is_safe_tune`
  forced false → SRE), the compliance/residency veto, the IaC blast-radius
  floor (critical-resource drift → SRE), the commitments savings floor
  (fail-closed to zero), and the budget-overage threshold. Each re-derives
  its verdict from the source store, so a prompt jailbreak can't clear it.
- **WORM audit terminus** on every terminal branch (apply / reject / hold /
  zero-savings / clean drift).
- **yields↔prompt contract**: every `llm` agent carries an OUTPUT CONTRACT
  block listing all yield keys.
- **Manager escalation**: `cto_human` (human_agent), PagerDuty webhook,
  Slack channel.

## Config pointer

The mesh is `configs/config.yaml`. Six agency modules carry the Python that
must be Python — a `@pre_compose` context pull and/or a deterministic
`@chain` floor: `cost_anomaly_agent.py` (safety gate + stash),
`commitments_agent.py` (savings floor + compose), `iac_drift_agent.py`
(blast-radius floor), `regulatory_monitor_agent.py` (veto floor),
`finops_governance_agent.py` (MCP shaper + budget floor), and
`cost_tuner_agent.py` (fail-closed + idempotent actuator). The seeded dev
store is `agency/_shared/store.py`; the WORM sink is
`agency/_shared/audit_logger.py`. Everything else is YAML.

## Reflection / decisions

- **One analyst, not three**: cost-anomaly + right-sizing merged into one
  LLM call; the former reserved-capacity planner + utilisation reviewer
  merged into `commitments_agent`. Smallest cast that still reads as a
  department.
- **State in the customer's systems**: spend/inventory come from the cloud
  billing API over MCP, not a template SQLite file. The compliance veto
  ledger moved to cross-session agent memory. The **seeded dev store**
  (`agency/_shared/store.py`) is the day-0 stand-in for every connector —
  each read names its production swap; it is never the system of record.
- **Floors over prompts**: the auto-apply gate, the residency veto, the
  critical-drift escalation, the budget-overage threshold and the savings
  total were prompt rules before — now each is a deterministic `@chain`
  floor that re-derives its verdict from the store, so the LLM can't waive
  it. Prompts still carry the rules for the *narrative*; the floor is the
  authority.
- **Slack split**: `SLACK_INFRA_CHANNEL` feeds the escalation target's
  `channel_id`; `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET` belong to a
  `channels:` block and are not auto-consumed here (left in `env` for when
  a `channels:` adapter is added).
- **finops_governance cron disabled by default** — a cron sweeping an
  unwired connector only fills the logs with errors.
