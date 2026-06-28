# infra-cost-ops — Infra Cost Ops

Continuous cloud-cost optimization as a department of agents. Cost
anomalies and right-sizing are classified-and-proposed in one LLM call,
risky tunes are gated by an SRE (then a CTO), safe tunes auto-apply, a
weekly commitments analyst reviews reserved-instance / savings-plan
utilisation, an IaC-drift reconciler watches intended-vs-actual state, a
compliance monitor vetoes residency/encryption-violating tunes, and a
FinOps-governance connector surfaces tag gaps and budget overages — every
applied change landing in a hash-chained WORM audit.

---

## 1. The story

**Maya, an SRE at a mid-size SaaS company**, never opens this system — it
runs without her until a decision needs her judgment. At 02:14 the cloud
cost-explorer fires an anomaly webhook: an `m5.4xlarge` running at 4% CPU
for nine days. `cost_anomaly_agent` classifies it in one call — type
`wasted_idle`, daily impact 41,000¢, production-tier `false`, and in the
same JSON proposes the tune: `m5.4xlarge → m5.large`, `is_safe_tune: true`,
`data_tier: false`. Because the proposal is safe and not data-tier, the
chain routes straight to `cost_tuner_agent`, which applies the resize
against the cloud API (dev: a stub), returns a `rollback_token`, and writes
the change to the WORM audit. Maya wakes up to a resolved ticket and an
audit line, not a 2 a.m. page.

The next morning a second anomaly lands — an `rds` instance flagged
`drift`. This one is `data_tier: true`, so the prompt's HARD SAFEGUARDS
force `is_safe_tune: false` and the chain routes to **Maya** (`sre_human`).
`cost_anomaly_agent` already stashed the full tune payload keyed by session
*before* the gate, so when Maya clicks **Approve** in her inbox, the SDK
resumes `cost_tuner_agent` with only the human-response meta — and the
tuner recovers the proposed spec from that stash and applies it. Had she
not responded within four hours, the gate's default-deny fallback escalates
to **the CTO** (`cto_human`); a CTO no-show holds the change for review.
Nothing irreversible happens without a human, and nothing is lost in the
hand-off.

## 2. Why it works in an organization

Cloud waste is continuous and granular — idle instances, over-provisioned
pods, under-used reservations, untagged spend, drift between Terraform and
reality. No human watches all of it, so it compounds. This agency turns
each waste signal into a *classified, costed, and (where safe)
auto-remediated* action, while reserving human attention for exactly the
changes that carry risk: production data tiers, SLA-affecting resizes,
critical-resource drift (auth / billing / security / kms / iam), and
compliance-sensitive region/encryption moves. The ROI is twofold:
**recovered spend** (right-sizing + reservation rebalancing run weekly, not
quarterly) and **avoided incidents** (a default-deny SRE→CTO gate and a
fail-closed actuator mean an unrecoverable or unreviewed change is never
applied). Every applied tune is hash-chained into a WORM log, so the
finance and security teams get an audit trail for free.

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
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | optional | Session store + auto-storage. Defaults to `localhost:6379`, no auth (`docker compose up redis -d`). |
| `ANTHROPIC_API_KEY` | optional | Only if you enable the built-in Claude summarizer for narration. |
| `PAGER_WEBHOOK` | optional | PagerDuty Events v2 endpoint for the manager's `webhook` escalation target. Defaults to the public enqueue URL. |

### Tier 3 — Connectors (wire when you adopt that capability)
| Var | Required | Purpose |
|---|---|---|
| `CLOUD_BILLING_MCP_URL` | optional until used | MCP server fronting your cloud billing/cost API (AWS Cost Explorer / GCP Cloud Billing / Azure Cost Management). Consumed by `finops_governance_agent`. After wiring, re-enable that agent's `wake_up: "0 8 * * *"` cron in `configs/config.yaml`. |
| `CLOUD_BILLING_MCP_TOKEN` | optional | Auth token for the billing MCP server. |
| `SLACK_INFRA_CHANNEL` | optional | Channel id for the manager's `channel`/`slack` escalation target (`message_template`). |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` | optional | These belong to a `channels: {slack: {...}}` block (a `ChannelConfig`) and are **not** auto-consumed by the escalation target — the `provider: slack` escalation target only reads `channel_id` + `message_template`. Add a `channels:` block if you want the SDK's Slack ingress/egress adapter. |

```bash
cp env .env && pip install -r requirements.txt
python validate_config.py        # pre-flight
python main.py                   # API at http://127.0.0.1:18820/docs
```

The connector env vars (`CLOUD_BILLING_MCP_URL` / `…_TOKEN`) are optional
day-0 — a **seeded dev store** (`agency/_shared/store.py`) stands in for
your cloud telemetry, commitment inventory, IaC drift state, workload
data-classes, and budgets so the full chain runs out of the box. It ships
a wasted-idle EC2 anomaly, a data-tier RDS anomaly, an under-used m5 RI, a
critical-resource VPC drift item, and a GDPR-bound workload. Every read is
the contract a production connector must satisfy; swap the body for the
real call and the agents are unchanged.

### Drive it — a cost anomaly end-to-end

```bash
# The seeded wasted_idle anomaly: 4% CPU for 9 days → the safety floor
# clears it (non-data-tier, low blast) → cost_tuner auto-applies → audit.
curl -X POST http://127.0.0.1:18820/api/mesh/call \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"cost_anomaly_webhook",
       "input_data":{"payload":{"tune_id":"tune-2026-0612-ec2-idle",
                     "cloud":"aws","resource_kind":"ec2"}}}'
```

`cost_anomaly_agent` pulls the telemetry + blast-radius via `@pre_compose`,
the `@chain` safety floor re-derives `data_tier`/`is_safe_tune`, and a safe
non-data-tier tune routes straight to `cost_tuner_agent` (a `rollback_token`
+ a hash-chained audit line). Swap the `tune_id` for
`tune-2026-0613-rds-overprov` and the floor forces the data-tier RDS resize
to **`sre_human`** instead — the LLM cannot wave it through.

```bash
# A GDPR-bound workload moved out of the EU — the @chain veto floor blocks it.
curl -X POST http://127.0.0.1:18820/api/mesh/call \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"compliance_veto_check",
       "input_data":{"tune_id":"tune-mv-1","workload_id":"orders-primary-db",
                     "proposed_change":{"kind":"region_change","to_value":"us-east-1"}}}'
```

```bash
# All four scenarios end-to-end (no server needed):
python examples/run_examples.py
```

## 4. Agents

| Agent | Role (job title) | Responsibilities | Can perform |
|---|---|---|---|
| `cost_anomaly_agent` | Cloud cost analyst | Classifies an anomaly (`spike`/`drift`/`idle`/`new_resource`/`wasted_idle`) **and** proposes the right-sizing tune in one call. `@pre_compose` pulls the telemetry + CMDB blast-radius; a deterministic `@chain` **safety-gate floor** re-derives `data_tier`/`blast_radius` and forces `is_safe_tune=false` on any data-tier / new_resource / critical-resource / over-cap tune (the LLM can only be *downgraded*); a second `@chain` stashes the floored payload pre-gate so an approval can resume the tuner. `llm`, `enforce_yields`, knowledge `cost_patterns`. | → `commitments_agent` (new_resource) · → `cost_tuner_agent` (safe & non-data-tier) · → `sre_human` (unsafe or data-tier) |
| `commitments_agent` | Cloud commitments analyst | Weekly RI/savings-plan utilisation review + reserved-capacity forecast. `@pre_compose` pulls the RI/SP inventory; a deterministic `@chain` **savings floor** recomputes wasted spend and fails closed (no basis → zero → audit, never the SRE); `@compose` sends the SRE the full forecast and the audit logger the terminus summary. `llm`, `enforce_yields`, cron `Mon 07:00`, knowledge `cloud_commit_catalog`. | → `sre_human` (savings > 0) · → `infra_audit_logger_agent` (zero savings) |
| `iac_drift_agent` | Infrastructure drift reconciler | Compares intended (Terraform/Pulumi) vs actual cloud state. `@pre_compose` pulls the last plan's drift set; a deterministic `@chain` **blast-radius floor** re-scans every item against the critical taxonomy (auth/billing/security/kms/iam/secrets) and forces `critical_drift=true` on any hit (the LLM can't clear it). `llm`, `enforce_yields`. | → `sre_human` (critical drift) · → `infra_audit_logger_agent` (clean) |
| `regulatory_monitor_agent` | Compliance officer (cloud) | Vetoes tunes violating data-residency / encryption-minimum mandates. `@pre_compose` pulls the workload's binding frameworks; a deterministic `@chain` **veto floor** re-runs the residency/encryption check and forces `vetoed=true` on any violation (fail-closed — the LLM can raise a veto, never clear one); the cross-session veto ledger lives in agent memory. `llm`, `enforce_yields`, `cross_session` memory. | → `sre_human` (vetoed) · → `cost_tuner_agent` (clean) |
| `cost_tuner_agent` | Cloud change actuator | Applies approved tunes against the cloud API (dev: stub). Fail-closed — refuses to apply when no tune payload is recoverable; **idempotent** — a re-fired approval for an already-applied `tune_id` is a no-op; returns a rollback token. `programmatic`, side-effecting module. | → `infra_audit_logger_agent` |
| `sre_human` | Site Reliability Engineer | Reviews risky tunes + critical drift; default-deny on a 4 h timeout (escalates to CTO). `human`, `fallback_on_timeout`. | → `cost_tuner_agent` (approve) · → `cto_human` (escalate) · → `infra_audit_logger_agent` (reject) |
| `cto_human` | Chief Technology Officer | Final authority on major-cost / data-tier changes; holds for review on an 8 h timeout. `human`, `fallback_on_timeout`. | → `cost_tuner_agent` (approve) · → `infra_audit_logger_agent` (otherwise) |
| `finops_governance_agent` | FinOps governance analyst | Tag-compliance sweeps + budget/forecast/overage alerts via your cloud billing/cost API over MCP. The backing module is the connector **shaper** (maps the raw MCP result into the yields, dev-store fallback when unwired) and enforces a deterministic `@chain` **budget-overage floor** (a forecast overage ≥ the threshold forces analyst action). `programmatic`, `integration: mcp`. | (terminal connector — results stored) |
| `infra_audit_logger_agent` | Audit & compliance recorder | Append-only hash-chained WORM record of every applied tune + decision. `external` (`framework: custom`), registered via `mesh.intelligence()` in `main.py`. | (audit terminus) |

The **manager** (`gpt-4o-mini`, learning router) supervises the mesh,
intervenes on its escalation rules (production data-tier touches,
cost-increasing or SLA-risking changes, critical-resource drift), and
escalates through three targets: `cto_human`, the PagerDuty webhook, and
the Slack channel.

See `PRD.md` for the architecture, flow diagram, and per-agent spec.
