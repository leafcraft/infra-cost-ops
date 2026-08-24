# infra-cost-ops — Infra Cost-Ops Pod — PRD

## Story

**Maya, an SRE at a mid-size SaaS company**, never opens this system — it runs
without her until a decision needs her judgment. A cost-explorer webhook fires;
**Cyra** (coordinator) opens the case and hands it to **Axel** (analyst, a
super-agent). Axel classifies the anomaly and proposes the tune in one pass; the
deterministic **safety floor** re-derives `data_tier`/`blast_radius`/
`is_safe_tune` from the resource kind + CMDB — a database, a critical-resource
fan-out, or an over-cap tune can never auto-apply, whatever the model scored. A
safe tune flows through **Vera** (compliance veto) to **Tucker** (the
fail-closed actuator), which re-checks authorisation, applies it, renders the
change record, and writes the WORM audit. A risky tune routes to **Maya**
(`sre_human`); Axel stashed the floored payload pre-gate, so on approval Tucker
recovers and applies it. A reject sends it back to Axel for a bounded
re-proposal; silence default-denies (`held_for_review` → audit) and the Manager
escalates to the **CTO**. Nothing irreversible happens without a human.

## Pod shape

**coordinator + specialists + a finisher + human members + a supervisor** — the
reference house style:

- **Coordinator** — `fin_coordinator_agent` (Cyra): owns the case, orchestrates,
  keeps status, single point of contact. Never does specialist work.
- **Specialists** — `cost_anomaly_agent` (Axel, classify + propose),
  `commitments_agent` (Rhea, RI/SP review), `iac_drift_agent` (Dref, drift),
  `regulatory_monitor_agent` (Vera, compliance veto), `finops_governance_agent`
  (Gwen, tag/budget governance, fanned out).
- **Finisher** — `cost_tuner_agent` (Tucker): the artifact-producing actuator
  AFTER the human/auto-apply gate.
- **Human members** — `sre_human` (Maya) and `cto_human` (accountable owner),
  first-class, reached by webhook, default-deny on timeout.
- **Supervisor** — the LeafMesh Manager (`routing: static`): reads every
  hand-off; stalls / contract violations / repeated errors escalate to the CTO.

## Flow

```
 cost_anomaly_webhook ─▶ fin_coordinator_agent ─(classify)─▶ cost_anomaly_agent
 case_status ──────────▶ fin_coordinator_agent                     │
 scheduled_rightsize ──────────────────────────────▶ cost_anomaly_agent (Axel)
 manual_action ────────────────────────────────────▶       │
                                                            │
                         new_resource ──▶ commitments_agent │
        safe & not data-tier ──▶ regulatory_monitor_agent (Vera)
                                       │ clean          │ vetoed
                                       ▼                ▼
                                 cost_tuner_agent    sre_human ─(approve)─▶ cost_tuner_agent
        risky ──────────────────────▶ sre_human (Maya)    │  │
                                       (approve)───────────┘  ├─(reject, bounded)─▶ cost_anomaly_agent
        re-proposal exhausted ──▶ audit                       └─(held / timeout)──▶ audit ─▶ [Manager ─▶ cto_human]

 iac_commit ──▶ iac_drift_agent ── critical ─▶ sre_human   ;  clean ─▶ audit
 weekly_commit_review / ri_expiry_alert / savings_plan_review ─▶ commitments_agent
        savings>0 ─▶ sre_human   ;   =0 ─▶ audit
 tag_compliance_sweep / budget_check ─▶ finops_governance_agent (×instances)
        action needed ─▶ sre_human   ;   clean ─▶ audit
 cost_tuner_agent ─▶ infra_audit_logger_agent   (hash-chained WORM terminus)
 cto_human ─(approve)─▶ cost_tuner_agent   ;   (else) ─▶ audit
```

## Modern capabilities (fitted to the domain)

- **super_agent** (DICT form) on `cost_anomaly_agent` — multi-step tuning:
  plan the tune, read the telemetry tools, verify the savings.
  `super_agent: {cost_ceiling: 100000, wall_clock: 900}`.
- **playbook** on `cost_anomaly_agent` — per-intent craft (`classify_anomaly`,
  `rightsize_tune`); only `description` rides in the prompt, `responsibility`
  loads on demand.
- **instances** on `finops_governance_agent` — `instances: 2` + an atomic
  `claim_resource_batch` tool fan the tag-sweep backlog across disjoint copies;
  a `@chain` reshape recomputes the ratio from the SUMMED counts post-merge.
- **mcp** — a COMMENTED `mcp:` block on `cost_anomaly_agent` (the SDK rejects an
  empty-URL http mcp at load, so it ships commented); dev-store tools carry
  day-0. Wire `CLOUDOPS_MCP_URL`, then uncomment.
- **timezone** — set next to every live `wake_up` (an un-timezoned cron fires in
  UTC): `cost_anomaly_agent` (daily), `commitments_agent` (Mon 07:00),
  `finops_governance_agent` (daily).

## Controls / trust

- **The finisher pattern** (`cost_tuner_agent`): (a) authorisation RE-CHECK — an
  approve-token allow-list + block-token deny-list, unknown/ambiguous **fails
  closed**, OR a deterministically-floored safe non-data-tier tune; (b)
  deterministic invariants — a recoverable spec, no double-apply (idempotent
  against the applied ledger); (c) renders the change record to `./out` and
  **deletes it on a blocked floor**; (d) two-layer defense — the body renders on
  its read, the `@chain` floor re-derives authorisation independently; (e)
  dev-store fallback that **announces itself** — it never fakes an ack.
- **Deterministic `@chain` floors** the LLM cannot waive, each re-deriving its
  verdict from the store: the safety gate (data-tier / new_resource / critical /
  over-cap → `is_safe_tune` false → SRE), the compliance/residency veto, the IaC
  blast-radius floor, the commitments savings floor (fail-closed to zero), and
  the FinOps coverage + budget-overage floors.
- **Default-deny HITL**: SRE (4 h) and CTO (8 h) gates fall back to
  `held_for_review` → audit; the Manager escalates. `dual` is on the human
  boundary agents ONLY — no `dual`→`dual` wire (an SRE stall reaches the CTO via
  the Manager, not a hardcoded human→human edge).
- **Bounded back-edge**: an SRE reject re-proposes via Axel, counted per session
  in the stash; past the cap `reproposal_exhausted` routes the turn to the audit
  terminus (Rule 3).
- **WORM audit terminus** on every terminal branch (apply / reject / hold /
  zero-savings / clean drift / exhausted loop), registered via
  `mesh.intelligence()` in `main.py`.
- **Multiple entry points** (12), one per real trigger; every wire is
  conditional on the producer's declared output.

## Data artifacts

- **CostEvent** (`data_structures`): `tune_id`, `cloud`, `resource_id`,
  `resource_kind`, `anomaly_type`, `received_at_utc` (required: `cloud`).
- **Tune payload** (stashed pre-gate by `cost_anomaly_agent`): `tune_id`,
  `current_spec`, `proposed_spec`, `daily_savings_cents`, `data_tier`,
  `is_safe_tune`, `blast_radius` — recovered by `cost_tuner_agent` after a HITL
  approval; the per-session `_counters` bound the re-proposal loop.
- **Applied-change record** (`./out/<tune_id>_applied_change.md`): the finisher's
  rendered artifact — proposed spec, rollback token, applied time.
- **Audit entry** (WORM): `audit_event_id`, `written_at_utc`, `session_id`,
  `payload`, `hash_chain_prev`, `hash_chain_current` (SHA-256 chain).
- **Veto ledger**: per-tune verdicts in `regulatory_monitor_agent` cross-session
  memory (`cross_session_limit: 100`).

## Modules (the Python that must be Python)

`configs/config.yaml` is the department. Six agency modules carry the
deterministic floors / actuator the LLM can't own; everything else is YAML.

- `cost_anomaly_agent.py` — safety-gate floor + pre-gate stash + bounded
  re-proposal counter (Axel reads telemetry via its tools; the floor re-derives).
- `commitments_agent.py` — savings floor (fail-closed to zero) + `@compose`.
- `iac_drift_agent.py` — blast-radius (critical-drift) floor.
- `regulatory_monitor_agent.py` — residency/encryption veto floor.
- `finops_governance_agent.py` — instances-merge coverage reshape + budget floor.
- `cost_tuner_agent.py` — the fail-closed, idempotent, artifact-producing finisher.

Shared: `agency/tools.py` (day-0 dev-store tools + the atomic claim tool),
`agency/_shared/store.py` (seeded dev store — the connector contract),
`agency/_shared/session_stash.py` (HITL bridge + bounded-loop counters),
`agency/_shared/audit_logger.py` (the hash-chained WORM sink).

## Reflection / decisions

- **Pod, not pipeline**: added Cyra (coordinator) and promoted `cost_tuner` to a
  true finisher (authorisation re-check, render → delete-on-block, dev-store
  fallback). The Manager became a supervisor (`routing: static`).
- **Compliance in the auto-apply path**: a safe tune flows through Vera before
  Tucker, so every unattended apply is compliance-checked; the finisher recovers
  `is_safe_tune` from the pre-gate stash to authorise it — a bare compliance
  check with no safety basis still fails closed.
- **Floors over prompts**: every risk decision (auto-apply safety, residency
  veto, critical-drift escalation, savings total, tag coverage, budget overage)
  is a deterministic `@chain` floor re-deriving its verdict from the store, so a
  prompt jailbreak can't clear it. Prompts carry the rules for the *narrative*;
  the floor is the authority.
- **State in the customer's systems**: telemetry / inventory / drift / tags /
  budgets come from the cloud APIs over MCP; the seeded dev store is the day-0
  stand-in for every connector and is never the system of record.
