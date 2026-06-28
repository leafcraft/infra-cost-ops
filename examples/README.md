# infra-cost-ops — example scenarios

Four realistic scenarios that exercise the major paths through this mesh.

## How to run

```bash
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 01_             # one scenario (substring match)
```

## Scenario map

- **01_ec2_idle_anomaly** — `manual_action` — Anomaly detector flags 6 EC2 instances at <5% CPU for 7 consecutive days — cost_anomaly_agent triages, rightsizing recommends shutdown candidates.
- **02_savings_plan_under_utilised** — `savings_plan_review` — Quarterly savings-plan review — utilisation dropped below 80%, commitments_agent quantifies impact + recommends rebalancing.
- **03_ri_expiry_30d** — `ri_expiry_alert` — Reserved Instance expires in 30 days — must renew, restructure, or let lapse. commitments_agent forecasts post-expiry on-demand cost.
- **04_iac_drift_detected** — `manual_action` — Terraform plan shows untracked changes in production VPC — IaC drift triggers SRE review.

## Audit trail

Every scenario appends one hash-chained entry to the template's
`logs/<audit>.jsonl` file. Tail it during a run to see the
ledger build up across scenarios.
