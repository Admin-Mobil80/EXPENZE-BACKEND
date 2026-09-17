# EXPENZE-BACKEND

The engine behind **expenze.ai**: intake from four channels, the model calls
that read a receipt, the deterministic policy engine that decides what it is
worth, and the API the portal and the BMS talk to.

## Part of Expenze

* **EXPENZE-PORTAL** — what customers use, at expenze.ai
* **EXPENZE-BMS** — what we run the platform from, at bms.expenze.ai
* **EXPENZE-BACKEND** — this

The three check out as siblings under one folder. This one currently deploys
the other two as well, which is the coupling the deployment work below removes.

## How a receipt becomes a decision

```
WhatsApp ─┐
Email ────┤
Portal ───┼─> intake ─> DynamoDB ─(stream)─> auditor ─> OpenAI: read the receipt
API ──────┘   1 credit                          │
                                                ├─> policy.py: decide it
                                                │   (deterministic, no model)
                                                └─> notify: tell the sender
```

The model reads; it never decides. `lambda_src/policy.py` is the only place a
reimbursement outcome is computed, and the portal carries a JavaScript port of
it so a reviewer sees a verdict recompute as they correct an expense type. The
port is a preview — the two must be kept in step, and the server's answer wins.

## Working in it

```bash
source .venv/bin/activate
python -m unittest discover -s tests -q      # 1,376 tests, ~1s
```

The virtualenv is tied to its absolute path. If this folder moves, delete
`.venv` and rebuild it from `requirements.txt`.

## Deploying

**Today this is a CDK app**, deployed from a laptop:

```bash
AWS_PROFILE=cloudmeter npx cdk deploy ExpensifyAI   # API, Lambdas, tables
AWS_PROFILE=cloudmeter npx cdk deploy ExpenzeSite   # ../PORTAL -> expenze.ai
AWS_PROFILE=cloudmeter npx cdk deploy ExpenzeBms    # ../BMS -> bms.expenze.ai
```

A site deploy is invisible until CloudFront is invalidated:

```bash
AWS_PROFILE=cloudmeter aws cloudfront create-invalidation \
  --distribution-id E3F4MLCVNAXPPE --paths "/*"
```

**Where it is going** — see `LAYOUT.md`:

* this backend moves to **SAM CLI**;
* PORTAL and BMS deploy themselves from their own repos, by **GitHub Actions**
  to S3, the way Flaunt already does — which also ends the oddity of a backend
  repo shipping two frontends.

Region, account, tagging and the reasoning behind each are in `LAYOUT.md`.
