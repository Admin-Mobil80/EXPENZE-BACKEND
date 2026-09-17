#!/usr/bin/env python3
"""CDK entrypoint for Expenze.

The account is pinned rather than inherited from whatever profile happens to be
active. In an account shared with a dozen live products, a deploy that silently
lands somewhere unintended is the expensive kind of mistake.

Two stacks, in two regions, for one reason:

  ExpenzeApi  (ap-southeast-1) - API Gateway, Lambda, DynamoDB
  ExpenzeSite (us-east-1)      - S3, CloudFront, ACM certificate

CloudFront only accepts a certificate issued in us-east-1, so the site lives
there rather than reaching across regions for a single certificate.
"""
import os

import aws_cdk as cdk

from expensifyai import tagging
from expensifyai.stack import ExpensifyAIStack
from expensifyai.site_stack import ExpenzeSiteStack

# Mobil80 shared account. Override only when moving Expenze to a dedicated
# account - at which point this is the single line that changes.
ACCOUNT = os.environ.get("EXPENZE_ACCOUNT", "231427841372")

# Singapore, deliberately away from ap-south-1 where CloudMeter lives.
REGION = os.environ.get("EXPENZE_REGION", "ap-southeast-1")

# The expenze.ai zone created 2026-09-08. Imported by id everywhere, never
# declared - declaring a zone that already exists is what produced the
# duplicate cloudmeter.io zone in this account.
ZONE_NAME = "expenze.ai"
ZONE_ID = "Z08768761ELNU80IGOYLG"

app = cdk.App()

# API custom domain is opt-in: `-c api_domain=api.expenze.ai`. Without it the
# execute-api URL is a complete, working endpoint.
API_DOMAIN = app.node.try_get_context("api_domain")

ExpensifyAIStack(
    app,
    "ExpensifyAI",
    domain_name=API_DOMAIN,
    hosted_zone_id=ZONE_ID if API_DOMAIN else None,
    zone_name=ZONE_NAME,
    otp_sender=f"noreply@{ZONE_NAME}",
    site_origins=[
        f"https://{ZONE_NAME}",
        f"https://www.{ZONE_NAME}",
        f"https://bms.{ZONE_NAME}",
    ],
    env=cdk.Environment(account=ACCOUNT, region=REGION),
    description="Expenze - agentic expense auditing API",
)

# Two applications, two CloudFront distributions, one shared hosted zone.
# us-east-1 for both: CloudFront certificates may only be issued there.
US_EAST_1 = cdk.Environment(account=ACCOUNT, region="us-east-1")

ExpenzeSiteStack(
    app,
    "ExpenzeSite",
    domain_name=ZONE_NAME,
    aliases=[f"www.{ZONE_NAME}"],
    zone_name=ZONE_NAME,
    hosted_zone_id=ZONE_ID,
    # A sibling of this one, not a folder inside it. The three pieces of
    # Expenze - the portal people use, the BMS we run it from, and this, which
    # deploys both - are separate trees under one product folder, so a path out
    # of BACKEND is how the backend reaches what it ships.
    source_dir="../PORTAL",
    bucket_suffix="site",
    env=US_EAST_1,
    description="Expenze - expenze.ai marketing site on CloudFront",
)

ExpenzeSiteStack(
    app,
    "ExpenzeBms",
    domain_name=f"bms.{ZONE_NAME}",
    zone_name=ZONE_NAME,
    hosted_zone_id=ZONE_ID,
    source_dir="../BMS",
    bucket_suffix="bms",
    env=US_EAST_1,
    description="Expenze - bms.expenze.ai application on CloudFront",
)

# Every resource is attributable on the shared invoice.
cdk.Tags.of(app).add("Project", "Expenze")
cdk.Tags.of(app).add("Owner", "riyad@mobil80.com")
cdk.Tags.of(app).add("ManagedBy", "cdk")

# And attributable *per service*, which is the part the invoice cannot infer.
# Account 231427841372 runs a dozen products, so its S3 line is every product's
# S3 - see expensifyai/tagging.py for the two keys and why there are two.
tagging.apply(app)

app.synth()
