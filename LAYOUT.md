# Expenze

Agentic expense auditing. A receipt arrives by WhatsApp, email, the portal or the
API; the agent reads it, measures it against the organisation's policy, and
either clears it for settlement or puts it in front of a named human.

Live at **expenze.ai**.

## Three trees, one product

```
EXPENSIFY.AI/
├── PORTAL/     expenze.ai        what customers use
├── BMS/        bms.expenze.ai    what we run the platform from
└── BACKEND/    the CDK app, the Lambdas, the policy engine, the tests
```

They are separated because they are edited for different reasons and by
different people. The portal changes when a reviewer needs something on screen;
the BMS changes when we need to see across customers; the backend changes when a
rule, a channel or a decision changes.

**BACKEND deploys the other two.** `app.py` points the site stacks at
`../PORTAL` and `../BMS`, so the three folders stay siblings — moving one
without the others breaks the deploy, and CDK will say so rather than shipping
an empty bucket.

## Working in it

Everything runs from `BACKEND/`:

```bash
cd BACKEND
source .venv/bin/activate
python -m unittest discover -s tests -q            # the whole suite
AWS_PROFILE=cloudmeter npx cdk deploy ExpensifyAI  # the API, Lambdas, tables
AWS_PROFILE=cloudmeter npx cdk deploy ExpenzeSite  # PORTAL -> expenze.ai
AWS_PROFILE=cloudmeter npx cdk deploy ExpenzeBms   # BMS -> bms.expenze.ai
```

A site deploy is not visible until CloudFront is invalidated:

```bash
AWS_PROFILE=cloudmeter aws cloudfront create-invalidation \
  --distribution-id E3F4MLCVNAXPPE --paths "/*"
```

The virtualenv is tied to its absolute path, so it is not portable: if this
folder moves again, delete `BACKEND/.venv` and rebuild it.

`BACKEND/README.md` has the architecture, the regions and the reasoning.
# ExpensifyAI

Agentic expense auditing: submit a receipt, get a reimbursement verdict with every
deduction explained.

**Status: proof of concept.** Deployed to `ap-southeast-1` (Singapore) inside the shared
Mobil80 account `231427841372`, deliberately away from CloudMeter in `ap-south-1`.

> Region separation keeps ExpensifyAI from getting mixed up with CloudMeter
> *operationally* — separate console, separate namespaces, separate Bedrock quota, no
> shared blast radius. It does **not** separate the bill or the IAM boundary; both still
> belong to the shared account. Every resource is tagged `Project=ExpensifyAI` so spend is
> attributable. A dedicated AWS account is the real fix if this graduates past PoC, and the
> stack is written to move there with a redeploy and one line changed in `app.py`.

## Architecture

```
POST /expenses ──> API Gateway (REST) ──> Lambda ──> OpenAI API
                                            │        (model from OPENAI_MODEL)
                                            ├──> Secrets Manager  (API key)
                                            └──> DynamoDB  ExpensifyAI-Expenses
```

**Why OpenAI and not Bedrock.** The stack was built on Amazon Bedrock first. Bedrock is
blocked on AWS account `231427841372` by `INVALID_PAYMENT_INSTRUMENT` at the AWS
Marketplace level — a billing state, not a code problem. One inference did succeed
(`global.anthropic.claude-opus-5`, 16 in / 4 out) before the payment check began failing,
so the region and model were correct; the account simply cannot be charged. Restoring
Bedrock later means attaching a valid payment instrument in the AWS console, then porting
`llm.py` back — `policy.py`, the tests, the schema, and the API contract are all
provider-agnostic and would not change.

Two model passes, for a reason:

1. **Extraction** — a *structured output*, not a tool. A tool whose implementation is "the
   model works it out" is a JSON schema wearing a tool costume: it costs a round trip and
   guarantees nothing. A strict `json_schema` response format returns a schema-valid
   receipt in one call.
2. **Audit** — a real function-calling loop. `evaluate_policy` is deterministic Python. The
   model supplies *judgment* (which line is alcohol, how many people ate); the code
   supplies the *arithmetic* and the verdict.

**The verdict always comes from `lambda_src/policy.py`.** The model's prose is attached as
a rationale and can never override the numbers. That boundary is the whole point — it means
an audit only has to trust one 200-line file, and the same receipt always produces the same
outcome.

## Policy rule #1

> Meals are capped per head, and alcohol is never reimbursable.

Chosen over a flat cap on the receipt total, which is a one-line `if` you can build an
entire architecture around and never discover it's wrong. This rule forces every hard part
to work on day one: line items must actually be extracted, each needs a category judgment,
headcount must be inferred *or admitted missing*, and the output shape is partial approval
(`reimbursable_total ≠ receipt_total`) rather than a boolean.

Caps live in `policy.py`, declared natively per currency:

| Currency | Per-head meal cap |
|---|---|
| INR (default) | ₹1,500 |
| USD | $18 |

A receipt in any other currency — SGD included — returns `needs_review` rather than being
converted at an invented exchange rate. Making up an FX rate in an expense system is a
correctness bug, not a shortcut. Adding SGD is a two-line change and an obvious next step
given where this is deployed.

Verdicts: `approved`, `partially_approved`, `rejected`, `needs_review`.

## Layout

| Path | What |
|---|---|
| `app.py` | CDK entrypoint; account and region pinned here |
| `expensifyai/stack.py` | DynamoDB, Lambda, layer, IAM, API Gateway |
| `lambda_src/policy.py` | The policy engine — the only place a verdict is decided |
| `lambda_src/handler.py` | Extraction pass, audit tool loop, request plumbing |
| `lambda_src/llm.py` | OpenAI access — the only provider-specific file |
| `lambda_src/secrets_loader.py` | Fetches and caches the API key |
| `scripts/build_layer.sh` | Builds the dependency layer without Docker |
| `tests/test_policy.py` | 11 tests over the policy engine, no AWS needed |
| `tests/test_schemas.py` | 3 tests that both JSON schemas satisfy OpenAI strict mode |
| `fixtures/` | Sample receipts, one per interesting verdict |

## Working on it

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m unittest discover -s tests -v
```

```bash
./scripts/build_layer.sh && cdk synth
```

There is no Docker on this machine and the only local Python is 3.9, so the usual CDK/SAM
Python bundling path — which shells out to a Linux container — is closed. `build_layer.sh`
pulls prebuilt `manylinux2014_aarch64` wheels for Python 3.13 directly instead. No
compiler, no container.

## Deploying

Needs `expensifyai` SSO credentials: https://m80.awsapps.com/start

```bash
aws sso login --profile expensifyai
```

`expensifyai` is this project's own AWS CLI profile — account `231427841372`, role
`AdminAccess`, region `ap-southeast-1`. It shares the `m80.awsapps.com` SSO login with the
other profiles on this machine (that is the company sign-in portal, not a project link),
but nothing in ExpensifyAI reads or writes any other product's resources.

```bash
./scripts/build_layer.sh && cdk deploy --profile expensifyai
```

Then set the API key. It is deliberately **not** in code, CloudFormation, or CDK context —
the stack creates the secret, you supply the value:

```bash
aws secretsmanager put-secret-value --secret-id expensifyai/openai-api-key --secret-string "$OPENAI_API_KEY" --profile expensifyai
```

Until that is set the API returns `503` naming the secret, rather than failing obscurely.

`ap-southeast-1` had no CDKToolkit stack, so it was bootstrapped once. That is a
**deliberate exception** to the standing rule against bootstrapping in this account: that
rule protects the *shared* CDKToolkit stacks in `ap-south-1` and `us-east-1`, which a
re-bootstrap can break for a dozen live products. Creating a fresh stack in a region that
had none touches nothing shared.

## Custom domain — `expensify.mobil80.com`

Testing needs no domain: the `execute-api` URL that `cdk deploy` prints is a complete,
working endpoint.

The apex `mobil80.com` zone is **not in this account** — 0 of its 19 zones. Every Mobil80
product instead gets a delegated subdomain zone (`markus.mobil80.com`,
`snappoll.mobil80.com`, and six more), each with its own nameservers, delegated from an
apex managed in a different AWS account. ExpensifyAI follows the same pattern, which means
it writes to no shared zone at all.

Rollout is staged, because requesting a certificate before the NS delegation exists would
leave `cdk deploy` blocking on a validation that can never succeed:

```bash
cdk deploy -c domain_stage=zone --profile expensifyai
```

That creates only the hosted zone and outputs `NameServersToDelegate`. Hand those to
whoever owns the apex account; they add one NS record for `expensify`. Then:

```bash
cdk deploy -c domain_stage=full --profile expensifyai
```

which adds the ACM certificate and a **regional** API Gateway custom domain. Regional
rather than edge-optimized on purpose: edge-optimized runs on CloudFront and would force
the certificate into us-east-1, pulling a second region in for no benefit at this scale.

Moving to a final domain later is cheap — API Gateway maps several custom domains to one
API, so the new name is added alongside and this one retires without the API, the Lambda,
or any client contract changing.

## Known gaps

- **Tax and service charge on excluded items are still claimable.** On a bar bill with a
  separate tax line, the tax on the alcohol is currently reimbursed. The prototype models
  the fix (apportion the pro-rata lines by the allowable share — on the Bombay Canteen
  fixture that moves the payout from ₹3,631.00 to ₹3,358.00); `policy.py` does not
  implement it yet.
- **Rules are hard-coded in `policy.py`.** The prototype's rules editor implies a stored,
  versioned policy — caps, excluded categories, autonomy — that the Lambda reads at
  invocation and stamps onto each decision. That table does not exist yet.

- **API Gateway REST caps the client-visible wait at 29s.** Two Opus 5 passes over a
  receipt image may exceed that. REST was chosen over an HTTP API precisely because its
  29s default *can* be raised via a service quota increase; the alternative is a 202 +
  poll flow against the existing `GET /expenses/{id}`. Real latency needs measuring before
  picking one.
- **DynamoDB grant is wider than the code uses.** CDK's `grant_read_write_data()` includes
  `DeleteItem`, `Scan` and the stream actions; the handler only calls `put_item` and
  `get_item`. Confined to this one table, but worth tightening before anything real.
- **Receipts leave AWS.** Calls go to `api.openai.com`, so receipt contents — which carry
  personal data — transit to a third party. Fine for a PoC with synthetic fixtures; needs a
  data-handling decision before real employee receipts.
- **`AWS::ApiGateway::Account` is a per-region singleton.** CDK adds it automatically to
  give API Gateway a CloudWatch Logs role, and it applies to *every* API Gateway in the
  account in that region — the one resource in this stack that isn't ExpensifyAI-private.
  In `ap-southeast-1` we are almost certainly the first API Gateway, so there is nothing to
  overwrite; verify with `aws apigateway get-account --region ap-southeast-1` before the
  first deploy. If something else is already there, set `cloud_watch_role=False` on the
  `LambdaRestApi` and lose only API Gateway execution logs.
- **No auth on the endpoint.** PoC only. Do not put a real receipt through it.
- Receipts carry personal data; the handler logs exceptions but returns opaque errors.
