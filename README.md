# Foundry Agents CI/CD

A GitHub Actions pipeline that promotes a Microsoft Foundry prompt agent from dev to production through quality, safety and human gates. Each gate fails closed.

```
PR / push ──► Gate 1a CI ──► deploy dev ──► Gate 1b quality eval
release tag ──► Gate 2 AI red team (reused if the agent is unchanged) ──► Gate 3 human approval ──► deploy prod + smoke + auto-rollback
```

| Gate | What it checks | When it runs |
|---|---|---|
| 1a CI | unit tests, config rendering, eval data and policy validation | every PR and push |
| 1b Quality | Foundry cloud evaluation: must-pass safety and scope cases, plus task adherence and intent resolution floors | push to `main`, release tags |
| 2 Red team | Foundry AI Red Teaming per risk category, with attack-success-rate limits and sample-size bounds | release tags only (it's expensive); a passing scan of the same agent fingerprint from the last 30 days is reused |
| 3 Approval | required reviewers on the `prod` GitHub environment | before every prod deploy |

## Contents

- [cicd/](cicd/): agent-as-code, eval datasets, thresholds, the gate CLIs and tests. **Start with [cicd/README.md](cicd/README.md)** for setup and the runbook.
- [.github/workflows/](.github/workflows/): the pipeline (`shift-manager-cicd.yml`) and the reusable deploy workflow (`agent-deploy.yml`).
- [docs/](docs/): design diagrams (open the `.drawio` files at [app.diagrams.net](https://app.diagrams.net)) and a mock pipeline dashboard (`agent-cicd-dashboard.html`, illustrative data).

## Quick start

```bash
cd cicd
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
python -m pytest tests -q
```

Live gates need `az login` (or OIDC in Actions) with the **Foundry User** role on the target project. No endpoints are committed: copy [cicd/.env.example](cicd/.env.example) to `cicd/.env` for local runs, and set the same names as GitHub Environment variables for Actions. See [cicd/README.md](cicd/README.md).

## Status (v1)

- Verified live against the dev project: deploy, smoke test and Gate 1b (29/29 pass).
- Not yet verified live: Gate 2. Its decision logic is unit-tested offline against a real scan.
- To do before the first prod release: OIDC identities, the `dev` and `prod` environments with reviewers and variables, the prod model deployment, and RAI sign-off on thresholds.
