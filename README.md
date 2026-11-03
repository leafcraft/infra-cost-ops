# infra-cost-ops — Infra Cost-Ops Pod

Continuous cloud-cost tuning as a **pod** — a coordinator, four analysts, a
compliance officer, a fail-closed actuator, two human gates, and a supervisor.
An anomaly is opened as a case, classified and turned into a right-sizing tune
(blast-radius scored), compliance-vetoed, gated by an SRE on anything risky,
applied by a fail-closed actuator, and hash-chained into a WORM audit. The CTO
is the accountable owner: a stalled case and the Manager's error escalations go
to the CTO. Commitments, IaC-drift and FinOps-governance analysts run alongside the
tune chain. It runs **day-0 against a seeded dev store — no connector required**.

---

## 1. The story

**Maya, an SRE at a mid-size SaaS company**, never opens this system — it runs
without her until a decision needs her judgment. At 02:14 the cloud
cost-explorer fires an anomaly webhook. **Cyra**, the Cost-Case Coordinator,
opens the case and hands it to **Axel**, the Cost Analyst. Axel — a
*super-agent* that plans, reads its telemetry tools, and verifies — classifies
it in one pass: the nightly-ETL host `i-0a1b2c3d` (m5.4xlarge, $560.64 a
month) has averaged 3.4% CPU for a week — `wasted_idle`, production-tier
`false` — and proposes `m5.4xlarge → m5.large` ($70.08 a month, about $16 a
day saved), `is_safe_tune: true`. Because the deterministic **safety floor**
re-derives it as safe and non-data-tier, the tune flows through **Vera**
(compliance veto — a plain rightsize clears instantly) to **Tucker**, the
actuator, which re-checks authorisation, records the resize (day 0: in the
applied-change ledger — no cloud API is called until you wire one), returns a
`rollback_token`, renders the applied-change record, and writes the WORM
audit. Maya wakes to a resolved ticket, not a 2 a.m. page.

The next morning a second anomaly lands — the production orders database
(`db.r5.4xlarge` Multi-AZ, $2,920 a month, p95 CPU 22%). It is a database, so
Axel's **safety floor forces `is_safe_tune: false`** (a database is never
auto-tuned, whatever the model scored) and the chain routes to **Maya**
(`sre_human`) with the proposal `db.r5.2xlarge` — one step down, so p95 stays
near 44%. Axel already stashed the floored tune payload keyed by session
*before* the gate, so when Maya replies **approve**, Tucker recovers the spec
from that stash and applies it. If Maya rejects, it goes back to Axel for a more
conservative re-proposal — **bounded**: after two rejects the loop terminates
into the audit sink, never forever. If Maya never answers within four hours,
the request waits no longer: its Inbox card closes and the ticket records that
no reply came. Nothing is applied, and nobody is chased automatically. Nothing
irreversible happens without a human, and nothing is lost across a hand-off.

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
not quarterly) and **avoided incidents** (an SRE gate that never approves on
silence, a deterministic safety floor the LLM can't waive, and a fail-closed actuator mean
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
| *(vault connection, any name)* | optional | The SRE / CTO gates answer in Studio's Inbox. To notify an external tool instead, create a vault connection (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook') and set `webhook_config: {connection: <name>}` on the seat. |
| *(vault connection `escalation_webhook`)* | optional | PagerDuty Events v2 endpoint for the Manager's `webhook` escalation target: create vault connection `escalation_webhook` (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook'), then uncomment the webhook target — in `configs/config.yaml` before the first start, in Studio's YAML editor after it (see *Changing the configuration after the first start*). |

### Tier 3 — Connectors (wire when you adopt that capability)
| Var | Required | Purpose |
|---|---|---|
| *(vault connection `cloudops_mcp`)* | optional until used | MCP server fronting Cost Explorer + CloudWatch + CMDB (its address + token, entered only in the vault — Infrastructure → Integrations → MCP). Wires `cost_anomaly_agent`'s **commented `mcp:` block**, which already picks `connection: cloudops_mcp` — create the connection, then uncomment it (in `configs/config.yaml` before the first start, in Studio's YAML editor after it; applies live, no restart). |
| *(Slack, via the vault)* | optional | A Slack channel for the Manager's error escalations ships commented. Connect Slack on Infrastructure → Messaging → Slack (bot token + signing secret live there, never in `.env`), give a human seat `channels: {slack: {connection: <name>}}`, then uncomment the `type: channel` target under `manager.escalation` with your channel id. |

The day-0 tools check the vault for the `cloudops_mcp` record to know whether a
real cloud connector is wired; nothing in `.env` switches them.

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
through. The SRE's approval request lands in **Studio → Inbox** — answer it there. `python examples/run_examples.py` runs the bundled scenarios end-to-end.

## 4. Agents

| Agent | Role (human/job title) | Responsibilities | Can perform |
|---|---|---|---|
| `fin_coordinator_agent` (Cyra) | Cost-Case Coordinator | Opens each tune case, orchestrates the pod, keeps live status, single point of contact. Never classifies/tunes/approves. `llm`, pure YAML. | → `cost_anomaly_agent` (classify) · → `cto_human` (escalate) |
| `cost_anomaly_agent` (Axel) | Cloud Cost Analyst | Classifies the anomaly **and** proposes the right-sizing tune in one call. `super_agent` (plan/execute/verify); reads telemetry + CMDB via dev-store tools; a `playbook` carries the craft on demand; the deterministic `@chain` **safety floor** re-derives `data_tier`/`blast_radius`/`is_safe_tune` and can only DOWNGRADE the LLM; a second `@chain` stashes the floored payload pre-gate. Daily wake (timezoned). | → `commitments_agent` (new_resource) · → `regulatory_monitor_agent` (safe & non-data-tier) · → `sre_human` (risky) · → audit (re-proposal exhausted) |
| `commitments_agent` (Rhea) | Cloud Commitments Analyst | Weekly RI/SP utilisation review + reserved-capacity forecast. `@chain` **savings floor** recomputes wasted spend and fails closed (no basis → zero → audit, never the SRE); `@compose` gives each callee only what it needs. `llm`, weekly wake (timezoned). | → `sre_human` (savings > 0) · → `infra_audit_logger_agent` (zero) |
| `iac_drift_agent` (Dref) | Infrastructure Drift Reconciler | Compares intended (Terraform/Pulumi) vs actual state. `@chain` **blast-radius floor** re-scans every item against the critical taxonomy (auth/billing/security/kms/iam) and forces `critical_drift=true` on any hit. `llm`. | → `sre_human` (critical) · → `infra_audit_logger_agent` (clean) |
| `regulatory_monitor_agent` (Vera) | Compliance Officer (cloud) | Vetoes tunes violating residency / encryption mandates. `@chain` **veto floor** re-runs the checks and forces `vetoed=true` on any violation (fail-closed — raise a veto, never clear one); cross-session veto ledger in agent memory. `llm`. | → `sre_human` (vetoed) · → `cost_tuner_agent` (clean) |
| `finops_governance_agent` (Gwen) | FinOps Governance Analyst | Tag-compliance sweep (fanned out with `scaling.copies.count: 2` + an **atomic claim tool** so copies split the backlog) + budget/forecast/overage alerts. `@chain` recomputes the ratio from the SUMMED counts post-merge and enforces the **budget-overage floor**. `llm`, daily wake (timezoned). | → `sre_human` (action needed) · → `infra_audit_logger_agent` (clean) |
| `cost_tuner_agent` (Tucker) | Cloud Change Actuator (finisher) | Applies an authorised tune (day 0: records it in the applied-change ledger; no cloud API call). Re-checks authorisation (approve TOKEN allow/deny-lists, unknown → **fail closed**; OR a floored-safe tune), renders the change record to `./out`, **deletes it on a blocked floor**, and is **idempotent** (a re-fired approval is a no-op). `programmatic`. | → `infra_audit_logger_agent` (always) |
| `sre_human` (Maya) | Site Reliability Engineer | Reviews risky tunes, compliance vetoes, critical drift, commitment recommendations and tag / budget actions; approve / reject / hold (non-tune items: noted / hold). 4 h; nothing is applied on silence. `human`, `dual`, Studio Inbox. | → `cost_tuner_agent` (approve) · → `cost_anomaly_agent` (reject, bounded) · → `infra_audit_logger_agent` (any other reply) |
| `cto_human` | Chief Technology Officer | Accountable owner — gets stalled cases from Cyra and the Manager's error escalations; approve / reject / hold. 8 h; nothing is applied on silence. `human`, `dual`, Studio Inbox. | → `cost_tuner_agent` (approve) · → `infra_audit_logger_agent` (any other reply) |
| `infra_audit_logger_agent` | Audit & Compliance Recorder | Append-only hash-chained WORM record of every applied tune + decision + held gate. `external` (`framework: custom`), registered via `mesh.intelligence()` in `main.py`. | (audit terminus) |

Gwen's two copies split the tag-sweep backlog with her own atomic claim tool
(`claim_resource_batch`). leafmesh 2.4.484's `scaling.copies.mode: auto` with
`claim_tool:` could replace this manual split (the mesh would claim each item and
start a copy only when there is one); that switch is the owner's decision and is
not made in this template.

### When nobody answers

Nothing risky is applied on silence. If the SRE does not answer within four
hours (the CTO: eight), the request waits no longer: its Inbox card closes and
the ticket records that no reply came. Nobody is chased automatically. Both
seats keep `fallback_on_timeout` / `fallback_response: held_for_review` in
config as the declared intent; on leafmesh 2.4.486 a timeout on these seats
routes nothing.

The **Manager** (`gpt-4o-mini`, `routing: static`) is the **supervisor**, not a
router. It escalates to the CTO only on errors: a non-retryable error (sign-in,
bad request, bad config) the first time it happens, five failed agents in one
session, or restarts used up. Contract violations alert people; they do not
escalate. A Slack channel target and a PagerDuty webhook ship commented — each
needs its vault connection first.

See `PRD.md` for the flow diagram, controls, and per-agent spec.

## Changing the configuration after the first start

`configs/config.yaml` is only the first-start seed. On the first start (an empty store) the agency imports it as version 1 and from then on keeps its configuration in the store. After that the file is not read again: an edit to it is **not** applied, even after a restart (the boot log says "that edit was NOT applied").

To change a running agency:

- **Studio** — its settings pages, or the raw YAML editor.
- **API** — `PUT /api/yaml/raw` with an `X-API-Key` header and the body `{"yaml_content": "<the whole YAML>", "save_to_disk": true}`, or `PATCH /api/yaml/config` with `{"updates": {...}, "save_to_disk": true}`.

Each save is a new version and applies live on every pod — no restart. (A few settings, such as brokers, make the agency reload itself in place within seconds.) So wherever this README or a comment says "uncomment X in `configs/config.yaml`", do it in the file before the first start, or in Studio's YAML editor after it.

## Where the data store is set

From leafmesh 2.4.482 the store (Redis or Postgres) is set **only in `.env`** — never in `configs/config.yaml`. A `persistence:` block in the YAML is ignored with a warning on leafmesh 2.4.482–2.4.485; from 2.4.486 the agency refuses to start if that block differs from `.env`, and names each `.env` line to add. The project's `.env` sets the store explicitly (`LEAFMESH_STORE`, `LEAFMESH_AGENCY_NAME`, `REDIS_*`).

- Redis (default): `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`; optional `REDIS_DEFAULT_TTL`, `REDIS_SESSION_TTL`, `REDIS_SSL*`, `REDIS_CLUSTER_NODES`.
- Postgres instead: `LEAFMESH_STORE=postgres` plus `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (optional `POSTGRES_SSLMODE`).
- Or the whole server in one value: `REDIS_URL=redis://user:pw@host:6379/0` (`rediss://` turns TLS on) or `DATABASE_URL=postgresql://user:pw@host:5432/db`. The single names fill in what the URL leaves out.
- With Docker Compose the app reaches Redis as `redis` (the service name); compose sets that for you.
- Check it in Studio → **Infrastructure → Datastores**: it shows whether the store is reachable (never the address or password).

## Your vault key — back it up

`create-leafmesh` put a `LEAFMESH_VAULT_KEY` in your `.env` when it made this project. It is the master key that encrypts every connection and credential you store in the vault (in Redis / Postgres).

- **Back it up like a master password.** Lose it and every stored connection has to be entered again.
- **Never change or regenerate it** for an existing project — a new key locks the existing vault. To change it, use `leafmesh vault rotate-key`.
- Every copy of the app (every replica) must get the **same** value. Docker Compose passes it in from `.env` as an environment variable.
- Never commit `.env` (it is already in `.gitignore`).
