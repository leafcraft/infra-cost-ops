# infra-cost-ops — Infra Cost-Ops Pod

Continuous cloud-cost tuning as a **pod** — a coordinator, four analysts, a
compliance officer, a fail-closed actuator, two human gates, and a supervisor.
An anomaly is opened as a case, classified and turned into a right-sizing tune
(blast-radius scored), compliance-vetoed, gated by an SRE (then the CTO) on
anything risky, applied by a fail-closed actuator, and hash-chained into a WORM
audit. Commitments, IaC-drift and FinOps-governance analysts run alongside the
tune chain. It runs **day-0 against a seeded dev store — no connector required**.

---

## 1. The story

**Maya, an SRE at a mid-size SaaS company**, never opens this system — it runs
without her until a decision needs her judgment. At 02:14 the cloud
cost-explorer fires an anomaly webhook. **Cyra**, the Cost-Case Coordinator,
opens the case and hands it to **Axel**, the Cost Analyst. Axel — a
*super-agent* that plans, reads its telemetry tools, and verifies — classifies
it in one pass: type `wasted_idle`, 41,000¢/day, production-tier `false`, and
proposes the tune `m5.4xlarge → m5.large`, `is_safe_tune: true`. Because the
deterministic **safety floor** re-derives it as safe and non-data-tier, the tune
flows through **Vera** (compliance veto — a plain rightsize clears instantly) to
**Tucker**, the actuator, which re-checks authorisation, applies the resize
against the cloud API (dev: a stub), returns a `rollback_token`, renders the
applied-change record, and writes the WORM audit. Maya wakes to a resolved
ticket, not a 2 a.m. page.

The next morning a second anomaly lands — an `rds` instance. It is a database,
so Axel's **safety floor forces `is_safe_tune: false`** (a database is never
auto-tuned, whatever the model scored) and the chain routes to **Maya**
(`sre_human`). Axel already stashed the floored tune payload keyed by session
*before* the gate, so when Maya clicks **Approve**, Tucker recovers the spec
from that stash and applies it. If Maya rejects, it goes back to Axel for a more
conservative re-proposal — **bounded**: after two rejects the loop terminates
into the audit sink, never forever. If Maya never answers, the gate **default-
denies** (`held_for_review` → audit) and the **Manager supervisor** escalates to
the **CTO**. Nothing irreversible happens without a human, and nothing is lost
across a hand-off.

## 2. Why it works in an organization

Cloud waste is continuous and granular — idle instances, over-provisioned pods,
under-used reservations, untagged spend, drift between Terraform and reality. No
human watches all of it, so it compounds. This **pod** turns each waste signal
into a *classified, costed, compliance-checked, and (where safe) auto-remediated*
action, while reserving human attention for exactly the changes that carry risk:
production data tiers, SLA-affecting resizes, critical-resource drift
(auth / billing / security / kms / iam), and residency/encryption-sensitive
moves. The discipline is the reference house style: **agents produce, humans
decide, systems record — and silence never advances anything.** The ROI is
twofold: **recovered spend** (right-sizing + reservation rebalancing run weekly,
not quarterly) and **avoided incidents** (a default-deny SRE→CTO gate, a
deterministic safety floor the LLM can't waive, and a fail-closed actuator mean
an unrecoverable, unreviewed, or non-compliant change is never applied). Every
applied tune is hash-chained into a WORM log, so finance and security get an
audit trail for free.

## 3. Configuration (three tiers)

### Tier 1 — Boot (required to start the mesh)
| Var | Required | Purpose |
|---|---|---|
| `LEAFMESH_LICENSE_KEY` | yes | Starts the SDK. |
| `LEAFMESH_ENV_TOKEN` | yes | Per-environment telemetry token (dev/staging/prod). |
| `OPENAI_API_KEY` | yes | Used by every `llm` agent (`gpt-4o-mini`). |

### Tier 2 — Host prerequisites + adapters
| Var | Required | Purpose |
|---|---|---|
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | optional | Session store. Defaults to `localhost:6379`, no auth (`docker compose up redis -d`). |
| `HITL_OUTBOUND_URL` | optional | Where the SRE / CTO gates POST review requests (your inbox / Slack bridge / on-call webhook). Defaults to a local stub. |
| `PAGER_WEBHOOK` | optional | PagerDuty Events v2 endpoint for the Manager's `webhook` escalation target. |

### Tier 3 — Connectors (wire when you adopt that capability)
| Var | Required | Purpose |
|---|---|---|
| `CLOUDOPS_MCP_URL` / `CLOUDOPS_MCP_TOKEN` | optional until used | MCP server fronting Cost Explorer + CloudWatch + CMDB. Wires `cost_anomaly_agent`'s **commented `mcp:` block** — fill the URL, then uncomment it in `configs/config.yaml`. |
| `CLOUD_BILLING_MCP_URL` / `CLOUD_BILLING_MCP_TOKEN` | optional until used | Cloud billing/cost API for `finops_governance_agent`'s tag/budget reads (dev: the seeded store). |
| `SLACK_INFRA_CHANNEL` | optional | Channel id for the Manager's `slack` escalation target. `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET` belong to a `channels:` block (not auto-consumed by the escalation target). |

```bash
cp env .env && pip install -r requirements.txt
python validate_config.py        # pre-flight — "all checks pass"
python main.py                   # API at http://127.0.0.1:18820/docs
```

The connector vars are optional day-0 — a **seeded dev store**
(`agency/_shared/store.py`) stands in for your cloud telemetry, commitment
inventory, IaC drift state, workload data-classes, tag backlog, and budgets, and
each day-0 tool (`agency/tools.py`) **announces itself** (`available:false`) when
it has no real source rather than faking a read. Every read is the contract a
production connector must satisfy; swap the body for the real call and the pod
is unchanged.

### Drive it — a cost anomaly end-to-end

```bash
# The seeded wasted_idle anomaly: the safety floor clears it (non-data-tier,
# low blast) -> compliance clears a plain rightsize -> Tucker auto-applies -> audit.
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"cost_anomaly_webhook",
       "data":{"payload":{"tune_id":"tune-2026-0612-ec2-idle",
               "cloud":"aws","resource_kind":"ec2"}}}'
```

Swap the `tune_id` for `tune-2026-0613-rds-overprov` and the safety floor forces
the data-tier RDS resize to **`sre_human`** instead — the LLM cannot wave it
through. `python examples/run_examples.py` runs the bundled scenarios end-to-end.

## 4. Agents

| Agent | Role (human/job title) | Responsibilities | Can perform |
|---|---|---|---|
| `fin_coordinator_agent` (Cyra) | Cost-Case Coordinator | Opens each tune case, orchestrates the pod, keeps live status, single point of contact. Never classifies/tunes/approves. `llm`, pure YAML. | → `cost_anomaly_agent` (classify) · → `cto_human` (escalate) |
| `cost_anomaly_agent` (Axel) | Cloud Cost Analyst | Classifies the anomaly **and** proposes the right-sizing tune in one call. `super_agent` (plan/execute/verify); reads telemetry + CMDB via dev-store tools; a `playbook` carries the craft on demand; the deterministic `@chain` **safety floor** re-derives `data_tier`/`blast_radius`/`is_safe_tune` and can only DOWNGRADE the LLM; a second `@chain` stashes the floored payload pre-gate. Daily wake (timezoned). | → `commitments_agent` (new_resource) · → `regulatory_monitor_agent` (safe & non-data-tier) · → `sre_human` (risky) · → audit (re-proposal exhausted) |
| `commitments_agent` (Rhea) | Cloud Commitments Analyst | Weekly RI/SP utilisation review + reserved-capacity forecast. `@chain` **savings floor** recomputes wasted spend and fails closed (no basis → zero → audit, never the SRE); `@compose` gives each callee only what it needs. `llm`, weekly wake (timezoned). | → `sre_human` (savings > 0) · → `infra_audit_logger_agent` (zero) |
| `iac_drift_agent` (Dref) | Infrastructure Drift Reconciler | Compares intended (Terraform/Pulumi) vs actual state. `@chain` **blast-radius floor** re-scans every item against the critical taxonomy (auth/billing/security/kms/iam) and forces `critical_drift=true` on any hit. `llm`. | → `sre_human` (critical) · → `infra_audit_logger_agent` (clean) |
| `regulatory_monitor_agent` (Vera) | Compliance Officer (cloud) | Vetoes tunes violating residency / encryption mandates. `@chain` **veto floor** re-runs the checks and forces `vetoed=true` on any violation (fail-closed — raise a veto, never clear one); cross-session veto ledger in agent memory. `llm`. | → `sre_human` (vetoed) · → `cost_tuner_agent` (clean) |
| `finops_governance_agent` (Gwen) | FinOps Governance Analyst | Tag-compliance sweep (fanned out with `instances: 2` + an **atomic claim tool** so copies split the backlog) + budget/forecast/overage alerts. `@chain` recomputes the ratio from the SUMMED counts post-merge and enforces the **budget-overage floor**. `llm`, daily wake (timezoned). | → `sre_human` (action needed) · → `infra_audit_logger_agent` (clean) |
| `cost_tuner_agent` (Tucker) | Cloud Change Actuator (finisher) | Applies an authorised tune against the cloud API (dev: stub). Re-checks authorisation (approve TOKEN allow/deny-lists, unknown → **fail closed**; OR a floored-safe tune), renders the change record to `./out`, **deletes it on a blocked floor**, and is **idempotent** (a re-fired approval is a no-op). `programmatic`. | → `infra_audit_logger_agent` (always) |
| `sre_human` (Maya) | Site Reliability Engineer | Reviews risky tunes + critical drift; approve / reject / hold. Default-deny on a 4 h timeout → audit; the Manager escalates to the CTO. `human`, `dual`, webhook. | → `cost_tuner_agent` (approve) · → `cost_anomaly_agent` (reject, bounded) · → `infra_audit_logger_agent` (held) |
| `cto_human` | Chief Technology Officer | Accountable owner — final authority on major-cost / data-tier changes; holds for review on an 8 h timeout. `human`, `dual`, webhook. | → `cost_tuner_agent` (approve) · → `infra_audit_logger_agent` (otherwise) |
| `infra_audit_logger_agent` | Audit & Compliance Recorder | Append-only hash-chained WORM record of every applied tune + decision + held gate. `external` (`framework: custom`), registered via `mesh.intelligence()` in `main.py`. | (audit terminus) |

The **Manager** (`gpt-4o-mini`, `routing: static`) is the **supervisor**, not a
router — it watches every hand-off and escalates stalls, contract violations, and
repeated errors to the CTO (plus a PagerDuty webhook and a Slack channel).

See `PRD.md` for the flow diagram, controls, and per-agent spec.
