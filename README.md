# Foundry Agents CI/CD

A GitHub Actions pipeline that promotes a Microsoft Foundry prompt agent from dev, through a test/UAT project, to production. On the way it passes security, residency, quality, Responsible AI and human gates. Each gate fails closed.

```
PR / push ──► G1 code & security ──► G2 residency + deploy dev ──► G3 evaluation (quality + RAI)
release tag ──► G2 + deploy test ──► G4 AI red team ──► approval packet + RAI scorecard
            ──► G5 UAT ratings + QA / business / RAI sign-offs ──► G6 release decision ──► G2 + deploy prod + smoke + auto-rollback
daily       ──► prod drift check
```

| Gate | What it checks | When it runs |
|---|---|---|
| G1 Code & security | secret scan (gitleaks), dependency scan (pip-audit), unit tests, config rendering, rule-file validation | every PR and push |
| G2 Config & residency | the environment's Azure setup against the customer rules in `policy.json` (presets: Canada strict, EU data zone, US, permissive; block or warn per rule) | before every deploy |
| G3 Evaluation | Foundry evaluation: must-pass behavior and privacy cases, document answers, citations, safety, groundedness, PII leaks, protected material | push to `main`, release tags (reused when unchanged) |
| G4 Red team | Foundry AI Red Teaming of the release candidate in the test project | release tags (reused when unchanged) |
| G5 UAT + sign-offs | testers' ratings of the exact release candidate in Foundry (human evaluation / trace 👍👎), then QA, business-owner and RAI approvals | release tags |
| G6 Release decision | all sign-offs present and recent, then the release manager approves `prod` | before every prod deploy |

## Contents

- [cicd/](cicd/): agent-as-code, customer rules, eval datasets, thresholds, the gate CLIs and tests. **Start with [cicd/README.md](cicd/README.md)** for setup, roles and the runbook.
- [.github/workflows/](.github/workflows/): the pipeline (`shift-manager-cicd.yml`), the reusable deploy workflow (`agent-deploy.yml`), the daily prod drift check (`prod-drift-check.yml`), portal-to-PR promotion (`promote-portal-agent.yml`) and the trace harvest that proposes new test cases from real conversations (`harvest-traces.yml`).
- [docs/](docs/): design diagrams (open the `.drawio` files at [app.diagrams.net](https://app.diagrams.net)). [agent-cicd-target.drawio](docs/agent-cicd-target.drawio) is the design this pipeline now implements, except identity persona tests (G4) and the ASSERT-generated test-case path. [agent-cicd-current.drawio](docs/agent-cicd-current.drawio) is the earlier dev → prod pipeline. Also a mock pipeline dashboard (`agent-cicd-dashboard.html`, illustrative data).

## Quick start

```bash
cd cicd
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
python -m pytest tests -q
```

Live gates need `az login` (or OIDC in Actions) with the **Foundry User** role on the target project. No endpoints are committed: copy [cicd/.env.example](cicd/.env.example) to `cicd/.env` for local runs, and set the same names as GitHub Environment secrets for Actions. See [cicd/README.md](cicd/README.md).

## Status

- Live in prod: `shiftmanageragent-promptv3` v1, released through the earlier dev → prod pipeline (`shift-manager-v1.0.2`).
- Built: test/UAT project, G1 scans, G2 residency rules (Canada strict, warn mode), G3 RAI checks, G5 UAT evidence and sign-offs, G6 release decision, drift check.
- Not built yet: identity persona tests (needs test users and a security-trimmed index), continuous evaluation in prod, ASSERT-generated test cases.