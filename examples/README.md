# infra-cost-ops — example scenarios

Four realistic scenarios that exercise the major paths through this mesh.

## How to run

```bash
create-leafmesh --load .   # once: loads the team into the Redis in .env (or use create-leafmesh --start .)
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 01_             # one scenario (substring match)
```

## Scenario map

- **01_ec2_idle_anomaly** — `manual_action` → `cost_anomaly_agent` — an idle EC2 anomaly: Axel classifies + proposes the resize; the safety floor clears it; compliance clears a plain rightsize; Tucker auto-applies.
- **02_savings_plan_under_utilised** — `savings_plan_review` → `commitments_agent` — utilisation dropped below 80%; Rhea quantifies impact + recommends rebalancing.
- **03_ri_expiry_30d** — `ri_expiry_alert` → `commitments_agent` — a Reserved Instance expires in 30 days; Rhea forecasts post-expiry on-demand cost.
- **04_iac_drift_detected** — `iac_commit` → `iac_drift_agent` — a Terraform plan shows untracked changes in a production VPC; Dref's blast-radius floor routes critical drift to the SRE.

## Audit trail

Every scenario appends one hash-chained entry to the template's
`logs/<audit>.jsonl` file. Tail it during a run to see the
ledger build up across scenarios.
